"""``dt-cloud`` CLI.

``build`` derives the sparse dir -> user attribution table from a marin
``scan_gcs`` objects listing (parquet). The listing never loads fully into
pandas: DuckDB pre-filters it down to the two small row sets the signals
need (distinct ``users/<seg>/`` prefixes and record-file rows).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict
from functools import partial
from pathlib import Path

import duckdb
import pandas as pd
from click import Choice, FloatRange, IntRange, UsageError, argument, group, option

from .identity import IDENTITIES_ENV, load_identities
from .site import DEFAULT_URL as SITE_DEFAULT_URL
from .secrets import env_secret, secret
from .index_footer import INDEX_VARIANTS
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


def _hard_exit() -> None:
    """Exit without interpreter teardown. The batch commands that stream GCS
    parquet through gcsfs hung at exit once (2026-09-08: last line printed,
    0% CPU for an hour) — fsspec's event loop being finalized while a file
    object's `__del__` still needs it. Every file is closed explicitly now;
    this is the guarantee."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)

err = partial(print, file=sys.stderr)


err = partial(print, file=sys.stderr)


@main.command()
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", required=True, type=Path, help="Output parquet path for the attribution table")
@option("-R", "--no-records", is_flag=True, help="Skip artifact-record mining (no GETs; path signals only)")
@option("-w", "--workers", default=16, help="Concurrent record reads")
def build(
    identities_path: str,
    listings: tuple[str, ...],
    out: Path,
    no_records: bool,
    workers: int,
) -> None:
    """Build the attribution table from a listing parquet."""
    identities = load_identities(identities_path)
    asof = dt.date.today()
    con = _connect()
    from disk_tree.listing import prepare_listing

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
    unknown_users = sorted({row.user for row in rows if row.user is not None and not identities.known(row.user)})
    if unknown_users:
        err(f"users not in {identities_path} (add name/github/aliases): {unknown_users}")


@main.command("executor-mine")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_path", type=Path, default=Path("tmp/executor-infos.parquet"), help="Output parquet")
@option("-w", "--workers", default=64, help="Concurrent GETs")
def executor_mine(listings: tuple[str, ...], out_path: Path, workers: int) -> None:
    """Targeted-GET mine of legacy `.executor_info` sidecars (name/output_path/config gs paths)."""
    from .executor_info import mine_executor_infos

    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    paths = [
        f"gs://{b}/{n}"
        for b, n in con.execute(
            f"SELECT DISTINCT bucket, name FROM {src} WHERE name LIKE '%.executor_info'"
        ).fetchall()
    ]
    mine_executor_infos(paths, out_path, max_workers=workers)


@main.command("wandb-attr")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", required=True, type=Path, help="Output parquet path for wandb attribution rows")
@option("-r", "--runs", "runs_path", required=True, type=Path, help="wandb-mine output parquet")
@option("-x", "--executor-infos", "executor_path", type=Path, default=None, help="executor-mine output parquet (adds executor-wandb rows)")
def wandb_attr(
    identities_path: str,
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
    from disk_tree.listing import prepare_listing

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
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=30, help="Rows in the per-user table")
@option("-u", "--user", "claim_user", default=None, help="Print this user's prefixes (inferred ownership, by bytes)")
def attr_report(
    attributions: tuple[str, ...],
    identities_path: str,
    listings: tuple[str, ...],
    top: int,
    claim_user: str | None,
) -> None:
    """Join listing × attribution (deepest-prefix-wins) → per-user bytes + coverage.

    Users are re-resolved against the *current* identities.yaml, so alias
    curation takes effect without rebuilding attribution parquets.
    """
    identities = load_identities(identities_path)
    con = _connect()
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    by_prefix = load_prefix_map(con, attributions, identities, src)

    dirs = con.execute(
        "SELECT bucket || '/' || CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END AS dir,"
        " sum(size_bytes) AS bytes, count(*) AS objects"
        f" FROM {src} GROUP BY dir"
    ).df()
    err(f"{len(dirs)} distinct dirs")

    from collections import defaultdict

    per_user: dict[str | None, list] = defaultdict(lambda: [0, 0])
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
        user, source = row if row else (None, "none")
        per_user[user][0] += int(nbytes)
        per_user[user][1] += int(objects)
        per_source[source][0] += int(nbytes)
        per_source[source][1] += int(objects)
        if claim_user is not None and user == claim_user:
            c = claim[prefix_of[dir_key]]
            c[0] += int(nbytes)
            c[1] += int(objects)

    print("== coverage by source ==")
    for source, (nbytes, objects) in sorted(per_source.items(), key=lambda kv: -kv[1][0]):
        print(f"{source:>16}  {nbytes/1e12:10.2f} TB  {objects:>12,} objects  {100*nbytes/total_bytes:5.1f}%")

    print(f"\n== top {top} users by bytes ('-' = nobody) ==")
    rows = sorted(per_user.items(), key=lambda kv: -kv[1][0])[:top]
    for user, (nbytes, objects) in rows:
        print(f"{user or '-':>24}  {nbytes/1e12:10.3f} TB  {objects:>12,} objects")

    if claim_user is not None:
        print(f"\n== claim list: {claim_user} ({len(claim)} prefixes) ==")
        for prefix, (nbytes, objects) in sorted(claim.items(), key=lambda kv: -kv[1][0]):
            print(f"{nbytes/1e9:12.2f} GB  {objects:>10,} objects  {prefix}")


@main.command()
@option("-a", "--attribution", "attributions", required=True, multiple=True, help="Attribution parquet(s); repeatable, concatenated")
@option("-d", "--depth", default=2, help="Prefix depth for the gap rollup (name components after bucket)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-n", "--top", default=40, help="Rows in the gap table")
def gaps(
    attributions: tuple[str, ...],
    depth: int,
    identities_path: str,
    listings: tuple[str, ...],
    top: int,
) -> None:
    """Largest *unattributed* prefixes at a given depth — the targeting list for
    new signals and `prefix_owners` curation."""
    identities = load_identities(identities_path)
    con = _connect()
    from disk_tree.listing import prepare_listing

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


@main.command("path-index")
@option("-a", "--attribution", "attributions", multiple=True, help="Attribution parquet(s); adds per-node user overlays")
@option("-c", "--dir-cache", "dir_cache", type=Path, default=None, help="Layer-2 cache dir (dir-stats/age-days parquet): attribution-independent rollups reused by re-attribution runs — see specs/dir-agg-cache.md")
@option("-d", "--asof", required=True, help="Scan date the listing came from (YYYY-MM-DD)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, default=None, help=f"identities.yaml path or URL, needed with -a (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s): scan_gcs or SII inventory schema; repeatable — earlier sources win per bucket")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Output dir for JSON files [default: site/public/data/<asof>]")
@option("-r", "--row-group-rows", default=None, type=int, help="Parquet row-group size for the store's sorts (default 8192 — the reader decodes ~one group per depth of a drilled subtree, so 32768 pushed small drills past its decode cap; specs/path-store.md §1.6)")
@option("-S", "--search", is_flag=True, help="Also write the search sidecars beside the `path` sort (`path-index.{rows,trigrams,rows-search}.parquet`: the filter view's segment-name index, specs/path-store-search.md); upload them with the generation dir")
@option("-u", "--user-sort-tiers", default=None, help="Only these sorts get a `-by-user` copy, comma-separated (`bysize`: the copy a lens view reads; default every sort)")
@option("-U", "--no-user-sorts", "user_sorts", is_flag=True, flag_value=False, default=True, help="Skip every `-by-user` sort copy (a lens then reads the mixed-user sorts, filtered per row — fine below the root, too wide at a user's root view; see -u)")
@option("-P", "--path-index", "path_index", type=Path, default=None, help="Write the path store here (`<dir>/path-index.parquet`, the `path` sort; `path-index-bysize.parquet` and the by-user copies land beside it — specs/path-store.md §4.3)")
@option("-x", "--access", "access", multiple=True, help="Access-log layer-2a agg parquet glob(s); adds per-node last-read ('a') for the read-recency lens")
def build_path_index(
    attributions: tuple[str, ...],
    dir_cache: Path | None,
    asof: str,
    identities_path: str | None,
    listings: tuple[str, ...],
    out_dir: Path | None,
    path_index: Path | None,
    row_group_rows: int | None,
    search: bool,
    user_sort_tiers: str | None,
    user_sorts: bool,
    access: tuple[str, ...],
) -> None:
    """Generate a dated site-data snapshot (tree/age/meta JSONs) from a listing.

    Snapshots live at site/public/data/<asof>/; the sibling scans.json index
    (dates, newest first — the site's scan dropdown) is refreshed afterwards.
    """
    import json
    import re

    from .viz import write_path_index

    if out_dir is None:
        out_dir = Path("site/public/data") / asof
    meta = write_path_index(
        listings, out_dir, asof, attributions, identities_path, access=access, dir_cache=dir_cache, path_index=path_index,
        user_sorts=user_sorts, row_group_rows=row_group_rows, search=search,
        user_sort_tiers=tuple(t for t in user_sort_tiers.split(",") if t) if user_sort_tiers else None,
    )
    err(f"wrote {out_dir}/: age.json meta.json ({meta['total_bytes']/1e12:.0f} TB, {meta['total_objects']:,} objects)")
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

    gcsfuse reads are slow (~20-50 MB/s) and path-index makes several passes over
    its inputs; staging to local NVMe first makes those passes local-speed.
    Already-staged files (same size) are skipped, so re-runs are idempotent.
    """
    from .stage import stage_globs

    stage_globs(globs, out_root, workers)


@main.command()
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-o", "--out", type=Path, default=None, help="Write rules JSON (users/aliases/prefix_owners + notes) for the site")
def rules(identities_path: str, out: Path | None) -> None:
    """Validate identities.yaml; optionally export it as site JSON.

    Checks alias collisions/shadowing and prefix_owners rows
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


# --- index tiers + their D1 footers (specs/view-serving.md, ported from gcs) ---


@main.command()
@option("-d", "--date", default=None, help="Scan date YYYY-MM-DD (default: latest from scans.json)")
@option("-f", "--max-age-days", default=2, type=int, help="Freshness: latest scan must be within this many days")
@option("-j", "--json", "as_json", is_flag=True, help="Emit machine-readable JSON to stdout")
@option("-s", "--subdir", default=None, help="Snapshot subdir under /data/ (default: $SNAPSHOTS_SUBDIR; `cw` for the CoreWeave deployment, empty for the default store)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {SITE_DEFAULT_URL})")
def healthcheck(date: str | None, max_age_days: int, as_json: bool, subdir: str | None, token: str | None, url: str | None) -> None:
    """Live-site health: is the latest scan actually *servable* end-to-end?

    Catches failures where the data pipeline succeeds but the site can't serve
    the scan — e.g. a path-index footer that never synced to D1, so the site
    footer-parses and 1102s (the 2026-08-31 /users outage). Checks scan
    freshness, subtree (the D1-index serving path), and the published data
    JSONs. Exits nonzero if any check fails — wire it into a cron /
    post-snapshot gate.
    """
    from .healthcheck import as_dict, run_checks
    from .site import creds

    base, tok = creds(token, url)
    sub = subdir if subdir is not None else os.environ.get("SNAPSHOTS_SUBDIR", "")
    resolved, checks = run_checks(base, tok, date, max_age_days=max_age_days, subdir=sub)
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


@main.command()
@option("-b", "--budget", default=None, type=float, help="Fail a scenario whose slowest request exceeds this many seconds")
@option("-c", "--cold", is_flag=True, help="Key subtree and diff reads past the edge cache (a random `minArea` ≈ the default), to measure uncached cost")
@option("-d", "--date", default=None, help="Query-set mode: the scan to query (default: the truth set's)")
@option("-j", "--json", "as_json", is_flag=True, help="Emit the run record (every request) as JSON to stdout")
@option("-k", "--only", multiple=True, help="Query-set mode: run only these query ids (repeatable)")
@option("-o", "--out", default=None, help="Write the run record to this path or prefix (`…/` or `gs://…/` → `<prefix><ts>.json`)")
@option("-q", "--hit", default=None, help="Filter term the filter-hit scenario searches for (default: the largest bucket's largest child)")
@option("-Q", "--queries", default=None, help="Query-set mode: a query set (YAML, local or gs://; `dt_cloud.bench.queryset`) to score against `-T`")
@option("-r", "--repeat", default=1, type=int, help="Query-set mode: requests per query × view (the first decides the verdict; timings are medians)")
@option("-s", "--subdir", default=None, help="Snapshot subdir under /data/ (default: $SNAPSHOTS_SUBDIR)")
@option("-S", "--serial", is_flag=True, help="Send each scenario's requests one at a time (default: concurrently, as a page load does)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN; none for a public deployment like r2.rbw.sh)")
@option("-T", "--truth", default=None, help="Query-set mode: the ground truth (`dt-cloud bench-truth -o`'s dir or gs:// prefix)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {SITE_DEFAULT_URL})")
@option("-x", "--param", "params", multiple=True, help="Query-set mode: extra query params for every request, e.g. `qe=box` (repeatable)")
def probe(budget: float | None, cold: bool, date: str | None, as_json: bool, only: tuple[str, ...], out: str | None, hit: str | None, queries: str | None, repeat: int, subdir: str | None, serial: bool, token: str | None, truth: str | None, url: str | None, params: tuple[str, ...]) -> None:
    """Replay the site's page loads (root, largest bucket, a matching and a
    non-matching path filter) against a live deployment.

    Each scenario's API requests go out concurrently, like the browser's, and
    each response's status, wall time, edge-cache tier and server time is
    recorded. Exits nonzero on any 5xx or transport failure (and on a scenario
    over `--budget`). `-o` keeps the record, so a prefix of runs is a latency
    time series.

    Query-set mode (`-Q queries.yml -T <truth>`): every query × view root of
    the set goes to `/api/subtree?q=…&full=1`, one request at a time, and is
    scored against the ground truth: the match-root set, the net totals, and
    the `partial` / `approximate` flags (an inexact answer must carry one; an
    unflagged inexact answer is a FAIL). Prints a table; exits nonzero on any
    FAIL or error.
    """
    import fsspec

    import random

    from .probe import cold_min_area, http_fetch, record, resolve_targets, run, scenarios, summarize
    from .site import creds

    base, tok = creds(token, url)
    if queries or truth:
        if not (queries and truth):
            raise UsageError("query-set mode needs both -Q and -T")
        from .bench import queryset, score as bs

        cases = queryset.load(queries)
        if only:
            unknown = set(only) - {c.id for c in cases}
            if unknown:
                raise UsageError(f"unknown query ids: {sorted(unknown)}")
            cases = [c for c in cases if c.id in only]
        tr = bs.Truth(truth)
        day = date or tr.summary["date"]
        engine = bs.SubtreeEngine(http_fetch(base, tok, timeout=300), day, params="&".join(params), cold=cold)
        err(f"probe -Q {base} @ {day}: {len(cases)} queries, {sum(len(c.views) for c in cases)} views{'; cold' if cold else ''}{f'; {params}' if params else ''}")
        err(bs.HEADER)
        scores = bs.run(engine, cases, tr, repeat=repeat, log=err)
        tally = bs.tally(scores)
        err("  ".join(f"{k}: {v}" for k, v in sorted(tally.items())))
        rec = bs.record(base, engine, day, truth, scores, cold=cold, repeat=repeat)
        if out:
            path = f"{out}{rec['ts']}.json" if out.endswith("/") else out
            with fsspec.open(path, "w") as fh:
                json.dump(rec, fh)
            err(f"wrote {path}")
        if as_json:
            print(json.dumps(rec, indent=2))
        if tally.get("FAIL") or tally.get("error"):
            raise SystemExit(1)
        return
    sub = subdir if subdir is not None else os.environ.get("SNAPSHOTS_SUBDIR", "")
    fetch = http_fetch(base, tok)
    t = resolve_targets(fetch, sub, hit)
    min_area = cold_min_area(random.Random()) if cold else ""
    err(f"probe {base} @ {t.date} (vs {t.prev}; bucket {t.bucket}; filter hit {t.hit!r}{'; cold' if cold else ''})")
    results = run(fetch, scenarios(t, min_area), parallel=not serial)
    ok, lines = summarize(results, round(budget * 1000) if budget is not None else None)
    for line in lines:
        err(line)
    err("PASS" if ok else "FAIL")
    rec = {**record(base, t, results), "cold": cold}
    if out:
        path = f"{out}{rec['ts']}.json" if out.endswith("/") else out
        with fsspec.open(path, "w") as fh:
            json.dump(rec, fh)
        err(f"wrote {path}")
    if as_json:
        print(json.dumps(rec, indent=2))
    if not ok:
        raise SystemExit(1)


@main.command("bench-truth")
@option("-a", "--append", is_flag=True, help="Add to (or replace queries in) an existing truth set at `-o` instead of rewriting its summary")
@option("-c", "--check", multiple=True, help="Also compute these query ids by a full `path` scan and compare (repeatable)")
@option("-k", "--only", multiple=True, help="Only these query ids (repeatable; with `-a`, to add queries to a set)")
@option("-m", "--mem", default="100GB", help="DuckDB memory limit")
@option("-N", "--no-names", is_flag=True, help="Scan for every query (don't use the generation's v1 names file)")
@option("-o", "--out", required=True, help="Output dir or gs:// prefix: `<id>.json` per query + `summary.json`")
@option("-s", "--stage", type=Path, default=None, help="Copy gs:// inputs here first (parallel ranged GETs; phase 0: copy, don't mount)")
@option("-t", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", default=None, help="DuckDB spill dir")
@argument("queries")
@argument("gen")
def bench_truth(append: bool, check: tuple[str, ...], only: tuple[str, ...], mem: str, no_names: bool, out: str, stage: Path | None, threads: int, tmp_dir: str | None, queries: str, gen: str) -> None:
    """Ground truth for a filter-bench query set over one index generation
    (GEN: its dir, local or gs://, holding `path-index.parquet` and, for the
    names-first method, the v1 `path-index.names.parquet`).

    For each query × view root: the match roots (count, md5 of the sorted
    list, and the list with each root's net totals when ≤ 50k), the outermost
    excluded paths, and the net totals — the Worker's filter semantics
    (specs/path-store-search.md §1). Heavy: run it on a VM beside the data
    (specs/filter-query-service.md §6 phase 1).
    """
    import time as _time

    import fsspec

    from .bench import queryset, truth as bt

    cases = queryset.load(queries)
    unknown = (set(check) | set(only)) - {c.id for c in cases}
    if unknown:
        raise UsageError(f"unknown query ids: {sorted(unknown)}")
    if only:
        cases = [c for c in cases if c.id in only]
    g = gen.rstrip("/")
    names = None if no_names else f"{g}/path-index.names.parquet"
    if names and not fsspec.core.url_to_fs(names)[0].exists(names):
        err(f"no names file at {names}: scanning for every query")
        names = None
    path_file = f"{g}/path-index.parquet"
    if stage:
        path_file = bt.local_or_download(path_file, stage / "path-index.parquet")
        names = names and bt.local_or_download(names, stage / "path-index.names.parquet")
    con = bt.connect(threads, mem, tmp_dir)
    t0 = _time.monotonic()
    truths = bt.compute(cases, path_file, names, list(check), con)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", gen)
    meta = {"date": m.group(1) if m else None, "gen": gen, "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "s": round(_time.monotonic() - t0, 1), "threads": threads, "queries_file": queries}
    bt.write(truths, out, meta, append=append)
    bad = [t.id for t in truths if t.check and not t.check["identical"]]
    err(f"wrote {out} ({len(truths)} queries, {meta['s']}s){f'; CHECK MISMATCH: {bad}' if bad else ''}")
    if bad:
        raise SystemExit(1)


@main.command("bench-index")
@option("-m", "--mem", default="90GB", help="DuckDB memory limit")
@option("-o", "--out", type=Path, required=True, help="Local dir for the index (`.npy` arrays + `detail.parquet` + `meta.json`)")
@option("-s", "--stage", type=Path, default=None, help="Copy gs:// inputs here first (parallel ranged GETs)")
@option("-t", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", default=None, help="DuckDB spill dir")
@option("-u", "--upload", default=None, help="Also upload the index to this gs:// prefix")
@argument("gen")
def bench_index(mem: str, out: Path, stage: Path | None, threads: int, tmp_dir: str | None, upload: str | None, gen: str) -> None:
    """Build the serving box's in-memory index (`dt_cloud.bench.mem`, format
    2) from one index generation (GEN: its dir, local or gs://, holding
    `path-index.parquet` and the v1 `path-index.names.parquet`). Heavy: a VM
    beside the data."""
    import fsspec

    from .bench import local, mem as bm

    g = gen.rstrip("/")
    path_file, names = f"{g}/path-index.parquet", f"{g}/path-index.names.parquet"
    if stage:
        path_file, _ = local.stage_file(path_file, stage / "path-index.parquet")
        names, _ = local.stage_file(names, stage / "path-index.names.parquet")
    meta = bm.build(path_file, names, out, threads=threads, mem=mem, tmp=tmp_dir)
    if upload:
        meta["upload"] = local.upload_dir(out, upload)
        (out / bm.META).write_text(json.dumps(meta, indent=1))
        with fsspec.open(f"{upload.rstrip('/')}/{bm.META}", "w") as fh:
            fh.write(json.dumps(meta, indent=1))
    print(json.dumps(meta, indent=1))


@main.command("bench-engine")
@option("-d", "--tmp-dir", default=None, help="DuckDB spill dir (`-e duckdb`)")
@option("-C", "--cold", is_flag=True, help="`-e ch`: drop ClickHouse's caches and the OS page cache before every answer (needs root)")
@option("-e", "--engine", type=Choice(["mem", "duckdb", "ch"]), required=True, help="`mem`: the custom in-memory index (`-i`); `duckdb`: names-first over GEN's parquet; `ch`: ClickHouse (`-U`; tables from `bench-ch-export`)")
@option("-E", "--evict", is_flag=True, help="Drop the index / generation files from the page cache before loading (a warm-from-disk load)")
@option("-i", "--index", default=None, help="`-e mem`: the index dir (local, a mount, or gs:// → copied to `-s` first)")
@option("-k", "--only", multiple=True, help="Run only these query ids (repeatable)")
@option("-M", "--mmap", is_flag=True, help="`-e mem`: map the index's arrays instead of reading them (pages load as queries touch them)")
@option("-m", "--mem", default="48GB", help="DuckDB memory limit (`-e duckdb`)")
@option("-n", "--name", default=None, help="Engine label in the record (e.g. `duckdb@gcsfuse`)")
@option("-o", "--out", default=None, help="Write the run record to this path or prefix (`…/` → `<prefix><name>-<ts>.json`)")
@option("-Q", "--queries", required=True, help="The query set (YAML, local or gs://)")
@option("-r", "--repeat", default=1, type=int, help="Answers per query × view (the first decides the verdict; timings are medians)")
@option("-s", "--stage", type=Path, default=None, help="Copy gs:// inputs here first (parallel ranged GETs)")
@option("-t", "--threads", default=16, type=int, help="Threads (vocabulary scan; DuckDB; ClickHouse `max_threads`)")
@option("-T", "--truth", required=True, help="The ground truth (`bench-truth -o`'s dir or gs:// prefix)")
@option("-U", "--url", default=None, help="`-e ch`: ClickHouse's HTTP endpoint (default http://localhost:8123)")
@argument("gen")
def bench_engine(cold: bool, tmp_dir: str | None, engine: str, evict: bool, index: str | None, only: tuple[str, ...], mmap: bool, mem: str, name: str | None, out: str | None, queries: str, repeat: int, stage: Path | None, threads: int, truth: str, url: str | None, gen: str) -> None:
    """Score a serving-box engine in-process against a query set's ground
    truth (specs/filter-query-service.md §6 phase 2): load time, resident
    memory, per-answer latency (roots + totals; root paths timed apart) and
    the verdicts `probe -Q` gives the Worker. Exits nonzero on any inexact
    answer or error.
    """
    import fsspec

    from .bench import local, mem as bm, queryset, score as bs, serve

    cases = queryset.load(queries)
    if only:
        unknown = set(only) - {c.id for c in cases}
        if unknown:
            raise UsageError(f"unknown query ids: {sorted(unknown)}")
        cases = [c for c in cases if c.id in only]
    tr = bs.Truth(truth)
    try:
        ix, kind, load = serve.load_engine(engine, gen, index=index, stage=stage, evict=evict, threads=threads, mem=mem, tmp_dir=tmp_dir, url=url, cold=cold, mmap=mmap)
    except ValueError as e:
        raise UsageError(str(e)) from e
    if engine == "mem":
        load["case_exceptions"] = ix.case_exceptions()
    label = name or engine
    err(f"bench-engine {label}: loaded in {load['total_s']}s; {json.dumps(load)}")
    eng = local.LocalEngine(kind, ix, name=label)
    err(bs.HEADER)
    scores = bs.run(eng, cases, tr, repeat=repeat, log=err)
    tally = bs.tally(scores)
    lat = local.latency_summary(eng.timings)
    err("  ".join(f"{k}: {v}" for k, v in sorted(tally.items())) + f"  {json.dumps(lat)}")
    rec = bs.record(f"local:{engine}", eng, tr.summary.get("date"), truth, scores, cold=False, repeat=repeat)
    rec.update(gen=gen, index=index, cold=cold, load=load, latency=lat, rss_end=bm.rss(), threads=threads, timings=[asdict(t) for t in eng.timings])
    if out:
        path = f"{out}{label}-{rec['ts']}.json" if out.endswith("/") else out
        with fsspec.open(path, "w") as fh:
            json.dump(rec, fh)
        err(f"wrote {path}")
    if set(tally) - {"exact"}:
        raise SystemExit(1)


@main.command("bench-ch-export")
@option("-i", "--index", required=True, help="The `mem` index dir built from GEN (local, or gs:// → copied to `-s` first)")
@option("-o", "--out", type=Path, required=True, help="Local output dir (`nodes-NNNN.parquet`, `names.parquet`)")
@option("-s", "--stage", type=Path, default=None, help="Copy gs:// inputs here first (parallel ranged GETs)")
@option("-u", "--upload", default=None, help="Also upload the output to this gs:// prefix")
@argument("gen")
def bench_ch_export(index: str, out: Path, stage: Path | None, upload: str | None, gen: str) -> None:
    """Export a generation as interval-encoded nodes for the ClickHouse engine
    (`dt_cloud.bench.ch`): each node's depth-first `pre` / `post`, name id,
    totals and path, plus the lowercase vocabulary. Needs the `mem` index's
    arrays in memory (~30 GB for gcs)."""
    from .bench import ch, local

    d = Path(index)
    if index.startswith("gs://"):
        if not stage:
            raise UsageError("a gs:// index needs -s")
        d = stage / "mem-index"
        d.mkdir(parents=True, exist_ok=True)
        for k in ("parent", "depth", "nid", "b", "o"):
            local.stage_file(f"{index.rstrip('/')}/{k}.npy", d / f"{k}.npy")
        local.stage_file(f"{index.rstrip('/')}/vocab.arrow", d / "vocab.arrow")
    path_file = f"{gen.rstrip('/')}/path-index.parquet"
    if stage:
        path_file, _ = local.stage_file(path_file, stage / "path-index.parquet")
    st = ch.export(d, path_file, out)
    if upload:
        st["upload"] = local.upload_dir(out, upload)
    print(json.dumps(st))


@main.command("bench-serve")
@option("-d", "--tmp-dir", default=None, help="DuckDB spill dir (`-e duckdb`)")
@option("-e", "--engine", type=Choice(["mem", "duckdb", "ch"]), required=True, help="As `bench-engine -e`")
@option("-E", "--evict", is_flag=True, help="Drop the index files from the page cache before loading")
@option("-H", "--host", default="0.0.0.0", help="Listen address")
@option("-i", "--index", default=None, help="`-e mem`: the index dir (local, a mount, or gs:// → copied to `-s` first)")
@option("-m", "--mem", default="48GB", help="DuckDB memory limit (`-e duckdb`)")
@option("-p", "--port", default=8765, type=int, help="Listen port")
@option("-Q", "--queries", required=True, help="The query set (YAML, local or gs://)")
@option("-s", "--stage", type=Path, default=None, help="Copy gs:// inputs here first (parallel ranged GETs)")
@option("-t", "--threads", default=16, type=int, help="Threads (vocabulary scan; DuckDB; ClickHouse `max_threads`)")
@option("-T", "--truth", required=True, help="The ground truth (`bench-truth -o`'s dir or gs:// prefix)")
@option("-U", "--url", default=None, help="`-e ch`: ClickHouse's HTTP endpoint (default http://localhost:8123)")
@argument("gen")
def bench_serve(tmp_dir: str | None, engine: str, evict: bool, host: str, index: str | None, mem: str, port: int, queries: str, stage: Path | None, threads: int, truth: str, url: str | None, gen: str) -> None:
    """Load an engine once and answer bench queries over HTTP (`GET /health`,
    `GET /run?k=<id>&r=N`): the long-lived process the suspend/resume and
    stop/start experiments time (specs/serving-options.md)."""
    from .bench import local, queryset, score as bs, serve

    cases = queryset.load(queries)
    tr = bs.Truth(truth)
    try:
        ix, kind, load = serve.load_engine(engine, gen, index=index, stage=stage, evict=evict, threads=threads, mem=mem, tmp_dir=tmp_dir, url=url)
    except ValueError as e:
        raise UsageError(str(e)) from e
    err(f"bench-serve {engine}: loaded in {load['total_s']}s; {json.dumps(load)}")
    serve.serve(local.LocalEngine(kind, ix, name=engine), cases, tr, load, host=host, port=port)


@main.command("serve-query")
@option("-2", "--diff", "n_latest", flag_value=2, default=1, help="`-e mem`: load the two latest scans (filtered diffs between them), not just the latest")
@option("-A", "--no-auth", is_flag=True, help="Serve without a bearer token (local use only)")
@option("-b", "--bind", default="0.0.0.0", help="Address to listen on")
@option("-B", "--db", default=None, help="`-e ch`: the store's database (default: $CLICKHOUSE_DB, else `default`)")
@option("-c", "--concurrency", type=IntRange(min=1), default=2, help="Maximum simultaneous backend responses, including streaming (default: 2; reduce on memory-constrained machines)")
@option("-C", "--dated-cold", is_flag=True, help="`-e ch -G GENERATION`: answer each dated scan's unregistered literals by bounded discovery over its completed name index (`ch-daily-name-index`); refuses to start if any is absent")
@option("-d", "--date", "dates", multiple=True, help="`-e mem`: load these scans (repeatable; default: the latest under ROOT)")
@option("-D", "--remote-detail", is_flag=True, help="`-e mem`: read a gs:// index's `detail.parquet` in place (ranged reads) instead of copying it")
@option("-e", "--engine", type=Choice(["mem", "ch"]), default="mem", help="`mem`: in-memory indexes under ROOT (filtered reads); `ch`: the ClickHouse store at ROOT's URL (every scan; plain and filtered reads, series)")
@option("-f", "--dated-name-store", help="`-e ch -L -G GENERATION`: explicit logical store binding for new dated roots")
@option("-g", "--hot-l1-generation", type=Path, help="`-e ch`: pin this published hot L1 catalog once; separate scan-free root-only route")
@option("-G", "--dated-l1-generation", type=Path, help="`-e ch -L -f STORE`: pin an accepted dated-root publication; new scans remain catalog-only")
@option("-H", "--hot-l2-artifact", type=Path, help="`-e ch -J CHECK`: pin an explicitly accepted paired L2 artifact; separate scan-free bucket-only route")
@option("-i", "--narrow-rich-name-index", "narrow_name_index", is_flag=True, help="`-e ch -N TARGET`: opt into its completed rich name-order index")
@option("-j", "--narrow-directory-parent-index", "narrow_parent_index", is_flag=True, help="`-e ch -N TARGET`: opt into its completed immutable directory parent index")
@option("-J", "--hot-l2-check", type=Path, help="`-e ch -H ARTIFACT`: matching complete L2 acceptance proof; required together")
@option("-k", "--narrow-plan", type=Choice(["legacy", "visible"]), default="legacy", help="`-e ch -N TARGET`: numeric serving plan; visible enables leaf/ancestor/fold reductions")
@option("-l", "--root-label", default=None, help="The store root's name in a tree (default: $ROOT_LABEL, else `marin GCS`)")
@option("-L", "--name-summary", is_flag=True, help="`-e ch -g GENERATION -N TARGET`: opt into stitched exact root summaries with bounded ordinary queries")
@option("-M", "--mmap", is_flag=True, help="`-e mem`: map the index's arrays instead of reading them (a tmpfs copy then costs its RAM once)")
@option("-N", "--narrow-target", help="`-e ch`: experimental numeric history for its bounded prefix/descendants and selected dates; canonical fallback elsewhere")
@option("-p", "--port", default=None, type=int, help="Port (default: $PORT, else 8080)")
@option("-r", "--root-plan", type=Choice(["rich", "compact"]), default="rich", help="`-e ch`: retain rich candidate aggregates (default), or discover positive roots with compact rows and reread their slices via a semijoin")
@option("-s", "--stage", type=Path, default=None, help="`-e mem`: copy gs:// indexes here first (on Cloud Run: an in-memory dir, with -M)")
@option("-t", "--threads", default=None, type=int, help="Vocabulary-scan threads / ClickHouse `max_threads` (default: the CPU count)")
@option("-T", "--token-env", default="QUERY_BOX_TOKEN", help="The env var holding the bearer token reads must present")
@option("-v", "--narrow-rich-name-variant", "narrow_name_variant", help="`-e ch -N TARGET -i`: select a completed isolated rich name-index variant (e.g. g64)")
@argument("root")
def serve_query(
    n_latest: int,
    no_auth: bool,
    bind: str,
    db: str | None,
    concurrency: int,
    dated_cold: bool,
    dates: tuple[str, ...],
    remote_detail: bool,
    engine: str,
    dated_name_store: str | None,
    hot_l1_generation: Path | None,
    dated_l1_generation: Path | None,
    hot_l2_artifact: Path | None,
    narrow_name_index: bool,
    narrow_parent_index: bool,
    hot_l2_check: Path | None,
    narrow_plan: str,
    root_label: str | None,
    name_summary: bool,
    mmap: bool,
    narrow_target: str | None,
    port: int | None,
    root_plan: str,
    stage: Path | None,
    threads: int | None,
    token_env: str,
    narrow_name_variant: str | None,
    root: str,
) -> None:
    """The serving box (specs/filter-query-service.md, specs/ch-store.md):
    answer the Worker's `/api/subtree` and `/api/diff` (and, `-e ch`,
    `/api/series`). `-e mem`: filtered reads from in-memory indexes
    (`bench-index`'s format 2), one per scan under ROOT (`<ROOT>/<date>/`,
    local or gs://); listens at once, `/healthz` reports `loading` until the
    scans are in memory. `-e ch`: ROOT is the ClickHouse HTTP URL of a store
    `ch-ingest` fills; every ingested scan is served."""
    import os as _os
    from threading import Semaphore

    from .box import server as bs

    token = None if no_auth else bs.token_from_env(token_env)
    if token is None and not no_auth:
        raise UsageError(f"${token_env} is unset (or pass -A to serve without auth)")
    label = root_label or _os.environ.get("ROOT_LABEL") or "marin GCS"
    syntax = _os.environ.get("QUERY_SYNTAX") or "simple"
    if hot_l1_generation is not None and engine != "ch":
        raise UsageError("--hot-l1-generation requires --engine ch")
    if name_summary and (engine != "ch" or hot_l1_generation is None or not narrow_target):
        raise UsageError("--name-summary requires --engine ch, --hot-l1-generation and --narrow-target")
    if (dated_l1_generation is None) != (dated_name_store is None):
        raise UsageError("--dated-l1-generation and --dated-name-store are required together")
    if dated_l1_generation is not None and (engine != "ch" or not name_summary):
        raise UsageError("--dated-l1-generation requires --engine ch and --name-summary")
    if dated_cold and dated_l1_generation is None:
        raise UsageError("--dated-cold requires --dated-l1-generation")
    if (hot_l2_artifact is None) != (hot_l2_check is None):
        raise UsageError("--hot-l2-artifact and --hot-l2-check are required together")
    if hot_l2_artifact is not None and engine != "ch":
        raise UsageError("--hot-l2-artifact/--hot-l2-check require --engine ch")
    if narrow_target and engine != "ch":
        raise UsageError("--narrow-target requires --engine ch")
    if narrow_name_index and (engine != "ch" or not narrow_target):
        raise UsageError("--narrow-rich-name-index requires --engine ch and --narrow-target")
    if narrow_parent_index and (engine != "ch" or not narrow_target):
        raise UsageError("--narrow-directory-parent-index requires --engine ch and --narrow-target")
    if narrow_plan != "legacy" and (engine != "ch" or not narrow_target):
        raise UsageError("--narrow-plan requires --engine ch and --narrow-target")
    if narrow_name_variant is not None and not narrow_name_index:
        raise UsageError("--narrow-rich-name-variant requires --narrow-rich-name-index")
    if engine == "ch":
        from .chstore.serve import Store

        box = bs.ChBox(Store(root, db=db or _os.environ.get("CLICKHOUSE_DB") or "default", threads=threads or _os.cpu_count() or 8, root_label=label,
                             syntax=syntax, root_plan=root_plan), gate=Semaphore(concurrency), narrow_target=narrow_target,
                       narrow_name_index=narrow_name_index, narrow_name_variant=narrow_name_variant, narrow_parent_index=narrow_parent_index,
                       narrow_plan=narrow_plan, hot_l1_generation=hot_l1_generation,
                       hot_l2_artifact=hot_l2_artifact, hot_l2_check=hot_l2_check, name_summary_enabled=name_summary,
                       dated_l1_generation=dated_l1_generation, dated_name_store=dated_name_store, dated_cold=dated_cold)
    else:
        box = bs.Box(
            root=root, dates=list(dates) or None, n_latest=n_latest, stage=stage, mmap=mmap, remote_detail=remote_detail,
            threads=threads or _os.cpu_count() or 8, root_label=label, syntax=syntax,
            gate=Semaphore(concurrency),
        )
    bs.serve(box, bind=bind, port=port or int(_os.environ.get("PORT") or 8080), token=token)


@main.command("ch-bench")
@option("-c", "--compare", "compare_to", default=None, help="Compare this run record (JSONL) with REQUESTS[0], another: per request, both p50 / max ms and matching bodies; nothing is sent")
@option("-C", "--cold", is_flag=True, help="Drop the box's ClickHouse caches and the OS page cache before each request (on the box, as root / privileged)")
@option("-d", "--date", default=None, help="With -Q: the scan the query set's requests ask")
@option("-f", "--file", "req_file", default=None, help="Requests, one `NAME=/api/…` per line (beside / instead of REQUESTS)")
@option("-j", "--parallel", default=1, type=IntRange(min=1), help="Maximum simultaneous HTTP clients; warm-only, recorded per request")
@option("-m", "--rss-pid", "rss_pids", multiple=True, type=IntRange(min=1), help="Same-host Linux PID to sample during requests (repeatable; requires -M)")
@option("-M", "--rss-out", type=Path, help="New JSONL file for 0.5-s RSS samples; existing files refused (requires -m)")
@option("-n", "--trials", default=1, type=int, help="Rounds over the requests")
@option("-o", "--out", default=None, help="Append each request's record (JSONL) here")
@option("-Q", "--queries", default=None, help="A bench query set (YAML): every query × view as a filtered subtree request (with -d)")
@option("-r", "--proc-root", type=Path, default=Path("/hostproc"), help="Linux proc mount for sampled PIDs; the dev wrapper exposes host /proc here")
@option("-s", "--seed", default=None, type=int, help="Jitter `minArea` per (seed, request, trial): past edge caches, the same in every run with this seed")
@option("-t", "--timeout", default=300.0, type=float, help="Per-request timeout, seconds")
@option("-T", "--token-env", default="QUERY_BOX_TOKEN", help="The env var holding the bearer token (none set = no auth)")
@option("-u", "--url", default="http://localhost:8080", help="Base URL: the box's serve-query, or a site")
@option("-U", "--ch-url", default=None, help="With -C: the box's ClickHouse (default http://localhost:8123)")
@argument("requests", nargs=-1)
def ch_bench(
    compare_to: str | None,
    cold: bool,
    date: str | None,
    req_file: str | None,
    parallel: int,
    rss_pids: tuple[int, ...],
    rss_out: Path | None,
    trials: int,
    out: str | None,
    queries: str | None,
    proc_root: Path,
    seed: int | None,
    timeout: float,
    token_env: str,
    url: str,
    ch_url: str | None,
    requests: tuple[str, ...],
) -> None:
    """Time requests against the box or a Worker, warm or cold, and compare
    two runs' latency and bodies (specs/ch-store.md §6). REQUESTS are
    `NAME=/api/…?…`."""
    import os as _os
    from contextlib import nullcontext

    from .chstore import bench as cb
    from .chstore.resources import RssMonitor

    if bool(rss_pids) != bool(rss_out):
        raise UsageError("--rss-pid and --rss-out are required together")
    if compare_to and rss_pids:
        raise UsageError("RSS monitoring is only for live benchmark runs")
    if compare_to:
        if len(requests) != 1:
            raise UsageError("-c A.jsonl B.jsonl")
        for row in cb.compare(cb.load(compare_to), cb.load(requests[0])):
            print(json.dumps(row))
        return
    if cold and parallel > 1:
        raise UsageError("independent per-request cold resets cannot overlap parallel requests")
    reqs = [tuple(r.split("=", 1)) for r in requests]
    if req_file:
        with open(req_file) as f:
            reqs += [tuple(line.rstrip("\n").split("=", 1)) for line in f if line.strip() and not line.startswith("#")]
    if queries:
        if not date:
            raise UsageError("-Q needs -d")
        reqs += cb.queryset_requests(queries, date)
    if not reqs:
        raise UsageError("no requests")
    observer = nullcontext()
    if rss_pids:
        assert rss_out is not None
        observer = RssMonitor(rss_pids, rss_out, proc_root=proc_root)
    with observer as monitor:
        recs = cb.run(url, reqs, token=_os.environ.get(token_env) or None, trials=trials, seed=seed, cold=cold, ch_url=ch_url, timeout=timeout, log=err, record_path=out, parallel=parallel)
    for row in cb.summary(recs):
        print(json.dumps(row))
    if monitor is not None:
        print(json.dumps({"resource_samples": monitor.summary()}))


@main.command("ch-narrow-build")
@option("-B", "--db", default="default", help="Source historical store database")
@option("-a", "--audit", "audit_after", is_flag=True, help="Run the full-domain read-only audit sequentially after successful construction; do not cut over serving")
@option("-b", "--snapshot-ranges", default=0, type=IntRange(min=0), help="Bound snapshot closure sets with N sampled key ranges (0: one whole-domain query)")
@option("-d", "--date", "dates", multiple=True, required=True, help="Frozen scan ids to include (repeatable)")
@option("-e", "--interval-engine", type=Choice(["numpy", "stream"]), default="numpy", help="Interval builder: N RAM arrays or external tree sort + O(depth) stack")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Stop before another build stage if disk free space falls below this reserve (not a hard quota)")
@option("-m", "--max-nodes", default=50_000_000, type=int, help="Refuse interval construction above this union size")
@option("-p", "--prefix", required=True, help="Frozen subtree including its root; pass an empty string for the global fleet")
@option("-R", "--resume-from", type=Choice(["snapshots-partial", "snapshots", "paths", "intervals", "names", "dictionary"]), help="Reuse a completed checkpoint; partial snapshots/names require matching successful query logs")
@option("-t", "--threads", default=8, type=int, help="ClickHouse threads")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@option("-u", "--union-engine", type=Choice(["group", "merge"]), default="group", help="Path union: hash aggregation or sorted full joins")
@argument("target")
def ch_narrow_build(
    db: str,
    audit_after: bool,
    snapshot_ranges: int,
    dates: tuple[str, ...],
    interval_engine: str,
    min_free_gib: int,
    max_nodes: int,
    prefix: str,
    resume_from: str | None,
    threads: int,
    url: str,
    union_engine: str,
    target: str,
) -> None:
    """Experimental frozen-scan ID/interval index. Creates new TARGET databases;
    refuses existing names, retains partial builds. Heavy: run on a dev node."""
    from .chstore.narrow import audit as audit_build, build
    from .chstore.serve import Store

    result = build(Store(url, db=db, threads=threads, timeout=7200), target, prefix, dates,
                   max_nodes=max_nodes, interval_engine=interval_engine, min_free_bytes=min_free_gib << 30,
                   union_engine=union_engine, resume_from=resume_from, snapshot_ranges=snapshot_ranges, log=err)
    if audit_after:
        err("full-domain audit: starting after successful construction")
        result = {**result, "audit": audit_build(url, target)}
        err("full-domain audit: passed")
    print(json.dumps(result))


@main.command("ch-narrow-dictionary-checkpoint")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_dictionary_checkpoint(url: str, target: str) -> None:
    """Explicit recovery for a completed pre-checkpoint dictionary; refuses an existing marker."""
    from .chstore.client import Ch
    from .chstore.narrow import dictionary_checkpoint

    ch = Ch(url, timeout=7200)
    try:
        print(json.dumps(dictionary_checkpoint(ch, target)))
    finally:
        ch.close()


@main.command("ch-narrow-intervals")
@option("-c", "--checkpoint", is_flag=True, help="Publish a completed hierarchy checkpoint (requires -T intervals and paths_manifest)")
@option("-e", "--order-engine", type=Choice(["segments", "escaped"]), default="escaped", help="Tree preorder sort key: segment array or equivalent escaped byte string")
@option("-p", "--prefix", required=True, help="Existing experimental union's root")
@option("-T", "--table", default="intervals_stream", help="New result table; refuses an existing table")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_intervals(
    checkpoint: bool,
    order_engine: str,
    prefix: str,
    table: str,
    url: str,
    target: str,
) -> None:
    """Benchmark disk-backed intervals over an existing experimental IDs table."""
    from .chstore.client import Ch
    from .chstore.narrow import stream_intervals

    ch = Ch(url, timeout=7200)
    try:
        print(json.dumps(stream_intervals(ch, target, prefix, table=table, order_engine=order_engine, checkpoint=checkpoint)))
    finally:
        ch.close()


@main.command("ch-narrow-bench")
@option("-c", "--compare", is_flag=True, help="Verify complete root identities and totals against the historical store")
@option("-C", "--cold", is_flag=True, help="Drop CH and OS caches before each experimental AND canonical discovery (dev node/root only)")
@option("-H", "--history", "historical", is_flag=True, help="Query the coalesced version tables, not the frozen snapshots")
@option("-M", "--no-metadata-paths", "metadata_paths", is_flag=True, flag_value=False, default=True, help="Component A/B: omit path strings from the timed rich payload read; NOT complete serving")
@option("-n", "--trials", default=2, type=int, help="Rounds per frozen scan")
@option("-o", "--out", type=Path, help="Append per-query JSONL records")
@option("-P", "--no-path-free", "path_free", is_flag=True, flag_value=False, default=True, help="Disable the numeric-only candidate shortcut for single literal substrings (A/B baseline)")
@option("-Q", "--queries", required=True, help="Benchmark query YAML; evaluate each query at the frozen subtree")
@option("-r", "--rich-name-index", "name_index", is_flag=True, help="Use the explicitly built rich name-order index for eligible literal roots (A/B)")
@option("-t", "--threads", default=8, type=int, help="ClickHouse threads")
@option("-T", "--truth", help="DATE=DIR: independent benchmark truth for one frozen scan")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_bench(
    compare: bool,
    cold: bool,
    historical: bool,
    metadata_paths: bool,
    trials: int,
    out: Path | None,
    path_free: bool,
    queries: str,
    name_index: bool,
    threads: int,
    truth: str | None,
    url: str,
    target: str,
) -> None:
    """Discovery + full-root materialization + late metadata; NOT treemap latency.
    Ignore the YAML's view list: this bounded experiment uses its one prefix."""
    from .bench.queryset import load
    from .bench.score import Truth
    from .chstore import narrow
    from .chstore.bench import drop_caches
    from .chstore.client import Ch
    from .chstore.serve import Store

    narrow.identifier(target)
    manifest = json.loads(Ch(url, db=target).scalar("SELECT doc FROM history_manifest" if historical else "SELECT doc FROM manifest"))
    store = Store(url, db=manifest["source_db"], threads=threads)
    truth_date, truth_set = None, None
    if truth:
        truth_date, sep, uri = truth.partition("=")
        if not sep or truth_date not in manifest["dates"] or not uri:
            raise UsageError("-T needs DATE=DIR for one of the frozen dates")
        truth_set = Truth(uri)
    expected = {}
    baseline = {}
    cases = load(queries)
    for trial in range(trials):
        for date, db in zip(manifest["dates"], manifest["dbs"], strict=True):
            for case in cases:
                if cold:
                    drop_caches(url)
                row = {"date": date, "query": case.id, "query_text": case.q, "syntax": case.qs, "trial": trial, "prefix": manifest["prefix"], "path_free": path_free, "cold": cold, "name_index": name_index, "threads": threads, "metadata_paths": metadata_paths}
                row.update(narrow.evaluate(url, db, manifest["prefix"], case.q, case.qs, threads, path_free=path_free, name_index=name_index, metadata_paths=metadata_paths))
                if compare:
                    key = date, case.id
                    if key not in expected or cold:
                        if cold:
                            drop_caches(url)
                        baseline[key] = {}
                        expected[key] = narrow.compare(store, date, manifest["prefix"], case.q, case.qs, timings=baseline[key])
                    actual = {k: row[k] for k in ("roots", "n", "md5", "b", "o")}
                    row["exact"] = actual == expected[key]
                    row["baseline"] = baseline[key]
                if truth_set is not None and date == truth_date:
                    # Some queries override the YAML's default views; their
                    # truth may not cover this experiment's frozen prefix.
                    want = next((v for v in truth_set.by_id[case.id]["views"] if v["view"] == manifest["prefix"]), None)
                    row["truth_covered"] = want is not None
                    if want is not None:
                        row["truth_exact"] = narrow.signature(row) == {"n": want["roots"], "md5": want["md5"], "b": want["bytes"], "o": want["objects"]}
                line = json.dumps(row)
                print(json.dumps({k: v for k, v in row.items() if k != "roots"}), flush=True)
                if out:
                    with out.open("a") as f:
                        f.write(line + "\n")
                if compare and not row["exact"]:
                    raise ValueError(f"narrow result mismatch: {date} / {case.id}; actual {narrow.signature(row)}, expected {narrow.signature(expected[key])}")
                if row.get("truth_exact") is False:
                    raise ValueError(f"independent truth mismatch: {date} / {case.id}; actual {narrow.signature(row)}, expected {want}")


@main.command("ch-raw-inventory")
@argument("path", type=Path)
def ch_raw_inventory(path: Path) -> None:
    """Summarize saved `gcloud storage ls -l` raw-shard metadata; no content reads."""
    from .chstore.inventory import raw_shards

    with path.open() as f:
        print(json.dumps(raw_shards(f), indent=2))


@main.command("ch-narrow-rich-name-index")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Disk reserve before building the rich access path")
@option("-g", "--granularity", default=8192, type=IntRange(min=1), help="Rows per rich name-index granule; smaller ranges trade read amplification for more marks/seeks")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@option("-v", "--variant", help="Isolated table/view/checkpoint suffix for granularity A/B; keeps the default index intact")
@argument("target")
def ch_narrow_rich_name_index(
    min_free_gib: int,
    granularity: int,
    url: str,
    variant: str | None,
    target: str,
) -> None:
    """Experimental rich (name-ID, path-ID, vf) index over an existing history."""
    from .chstore.narrow import rich_name_index

    print(json.dumps(rich_name_index(url, target, granularity=granularity, min_free_bytes=min_free_gib << 30, variant=variant)))


@main.command("ch-ancestry-build")
@option("-d", "--date", required=True, help="Selected scan date in an existing numeric history")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Disk reserve before the new experimental table")
@option("-T", "--table", default="ancestry_nodes", help="New name-ordered scalar table; refuses an existing table")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_ancestry_build(
    date: str,
    min_free_gib: int,
    table: str,
    url: str,
    target: str,
) -> None:
    """Experimental inline parent IDs; no incremental allocator or production writes."""
    from .chstore.ancestry import build

    print(json.dumps(build(url, target, date, table=table, min_free_bytes=min_free_gib << 30)))


@main.command("ch-narrow-numeric-parent-index")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Disk reserve before streaming all frozen path parent links")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_numeric_parent_index(
    min_free_gib: int,
    url: str,
    target: str,
) -> None:
    """Build immutable numeric parents once; no path decoding or production writes."""
    from .chstore.narrow import numeric_parent_index

    print(json.dumps(numeric_parent_index(url, target, min_free_bytes=min_free_gib << 30)))


@main.command("ch-narrow-parent-index")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Disk reserve before copying immutable directory parent IDs")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_parent_index(
    min_free_gib: int,
    url: str,
    target: str,
) -> None:
    """Build a compact frozen directory-parent access path; no production routing."""
    from .chstore.narrow import directory_parent_index

    print(json.dumps(directory_parent_index(url, target, min_free_bytes=min_free_gib << 30)))


@main.command("ch-narrow-parent-bench")
@option("-b", "--batch-rows", default=1_000_000, type=IntRange(min=1, max=1_000_000), help="Frozen preorder span per rich-source read")
@option("-j", "--numeric-join", default="grace_hash", type=Choice(["grace_hash", "full_sorting_merge"]), help="Numeric join algorithm; string baseline always uses grace_hash")
@option("-o", "--out", type=Path, help="Append each exact paired component result as JSONL")
@option("-s", "--start", "starts", multiple=True, required=True, type=IntRange(min=0), help="First frozen preorder key (repeat for several regions)")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_parent_bench(
    batch_rows: int,
    numeric_join: str,
    out: Path | None,
    starts: tuple[int, ...],
    url: str,
    target: str,
) -> None:
    """Compare rich-source parent joins; NOT full history construction or serving."""
    from .chstore.narrow import parent_benchmark

    for row in parent_benchmark(url, target, starts, batch_rows=batch_rows, numeric_join=numeric_join):
        print(json.dumps(row), flush=True)
        if out:
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")


@main.command("ch-ancestry-bench")
@option("-c", "--compare", is_flag=True, help="Verify uncapped roots and totals against canonical history")
@option("-C", "--cold", is_flag=True, help="Reset CH/OS caches independently before both engines (dev node/root only)")
@option("-n", "--trials", default=2, type=IntRange(min=1), help="Rounds per query")
@option("-o", "--out", type=Path, help="Append completed root fingerprints and timings as JSONL")
@option("-Q", "--queries", required=True, help="Single-literal query YAML; uses the experiment's prefix")
@option("-t", "--threads", default=8, type=IntRange(min=1), help="Same ClickHouse thread limit for experimental and canonical discovery")
@option("-T", "--table", default="ancestry_nodes", help="Completed experimental scalar table")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_ancestry_bench(
    compare: bool,
    cold: bool,
    trials: int,
    out: Path | None,
    queries: str,
    threads: int,
    table: str,
    url: str,
    target: str,
) -> None:
    """Opaque-ID ancestry discovery + fingerprints; NOT complete responses."""
    from .bench.queryset import load
    from .chstore import ancestry, narrow
    from .chstore.bench import drop_caches
    from .chstore.client import Ch
    from .chstore.serve import Store

    narrow.identifier(target)
    ch = Ch(url, db=target)
    try:
        source = json.loads(ch.scalar("SELECT doc FROM history_manifest"))["source_db"]
    finally:
        ch.close()
    store = Store(url, db=source, threads=threads)
    cases = load(queries)
    for trial in range(trials):
        for case in cases:
            if cold:
                drop_caches(url)
            row = {"query": case.id, "trial": trial, "cold": cold, "threads": threads,
                   **ancestry.evaluate(url, target, table, case.q, case.qs, threads=threads)}
            if compare:
                if cold:
                    drop_caches(url)
                timings = {}
                expected = narrow.compare(store, row["date"], row["prefix"], case.q, case.qs, timings=timings)
                row["exact"] = {k: row[k] for k in ("roots", "n", "md5", "b", "o")} == expected
                row["baseline"] = timings
            print(json.dumps({k: v for k, v in row.items() if k != "roots"}), flush=True)
            if out:
                with out.open("a") as f:
                    f.write(json.dumps(row) + "\n")
            if compare and not row["exact"]:
                raise ValueError(f"ancestry result mismatch: {row['date']} / {case.id}")


@main.command("ch-narrow-coalesce-bench")
@option("-b", "--batch-rows", default=1_000_000, type=IntRange(min=1, max=1_000_000), help="Bound each read-only preorder range")
@option("-k", "--table", default="metadata", type=Choice(["nodes", "metadata"]), help="Scalar or rich versions; metadata requires completed numeric parent links")
@option("-n", "--trials", default=2, type=IntRange(min=1), help="Rounds, alternating window/pair plan order")
@option("-o", "--out", type=Path, help="New JSONL file; refuses existing files")
@option("-s", "--start", "starts", multiple=True, required=True, type=IntRange(min=0), help="Range start ID; repeat for several bounded cases")
@option("-t", "--threads", default=2, type=IntRange(min=1), help="Equal query thread limit")
@option("-T", "--temp-dir", default="/data/tmp/ch-coalesce-bench", type=Path, help="Dev-node scratch for exact streamed comparison; temporary data removed on exit")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_coalesce_bench(
    batch_rows: int,
    table: str,
    trials: int,
    out: Path | None,
    starts: tuple[int, ...],
    threads: int,
    temp_dir: Path,
    url: str,
    target: str,
) -> None:
    """Read-only two-snapshot coalescing A/B on a dev node; no history publication."""
    from contextlib import nullcontext

    from .chstore.client import Ch
    from .chstore.coalesce import benchmark

    ch = Ch(url, db=target, timeout=7200)
    try:
        with out.open("x") if out else nullcontext(None) as stream:
            def emit(row: dict) -> None:
                line = json.dumps(row)
                print(line, flush=True)
                if stream is not None:
                    stream.write(line + "\n")
                    stream.flush()

            benchmark(ch, target, starts, table=table, batch_rows=batch_rows, threads=threads,
                      trials=trials, temp_dir=temp_dir, emit=emit)
    finally:
        ch.close()


@main.command("ch-narrow-history")
@option("-R", "--resume-publication", is_flag=True, help="Publish a completed streamed-hierarchy build only after exact query-log and row-count validation; never replay data writes")
@option("-a", "--numeric-ancestors", is_flag=True, help="Stream frozen directory ancestor arrays in one O(depth) pass instead of repeated string joins (A/B)")
@option("-b", "--batch-rows", default=1_000_000, type=IntRange(min=1), help="Preorder span per hierarchy/history batch; bounds aggregation and parent joins")
@option("-e", "--coalescer", default="window", type=Choice(["window", "pair"]), help="Opt-in pair plan requires exactly two selected scans; window supports general episodes")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Stop before another history stage if disk free space falls below this reserve (not a hard quota)")
@option("-j", "--build-threads", default=2, type=IntRange(min=1), help="Thread limit for bounded history joins/windows/sorts; keeps the 8-GiB query limit")
@option("-p", "--numeric-parents", is_flag=True, help="Build/reuse all frozen numeric parent links instead of repeated string-parent joins (A/B)")
@option("-t", "--threads", default=8, type=int, help="ClickHouse threads")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_history(
    resume_publication: bool,
    numeric_ancestors: bool,
    batch_rows: int,
    coalescer: str,
    min_free_gib: int,
    build_threads: int,
    numeric_parents: bool,
    threads: int,
    url: str,
    target: str,
) -> None:
    """Coalesce an experimental snapshot build into query-time SCD2 views."""
    from .chstore.narrow import history
    from .chstore.serve import Store

    print(json.dumps(history(Store(url, threads=threads, timeout=7200), target, batch_rows=batch_rows,
                             build_threads=build_threads, min_free_bytes=min_free_gib << 30, numeric_parents=numeric_parents,
                             numeric_ancestors=numeric_ancestors, coalescer=coalescer, resume_publication=resume_publication, log=err)))


@main.command("ch-narrow-hierarchy-stream")
@option("-f", "--min-free-gib", default=64, type=IntRange(min=0), help="Require this disk reserve before creating a new table (not a hard quota)")
@option("-T", "--table", default="hierarchy_stream", help="New result table; refuses existing or partial tables")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_hierarchy_stream(
    min_free_gib: int,
    table: str,
    url: str,
    target: str,
) -> None:
    """Build frozen numeric ancestors from completed parent_paths; run remotely."""
    from .chstore.client import Ch
    from .chstore.narrow import stream_hierarchy

    ch = Ch(url, timeout=7200)
    try:
        print(json.dumps(stream_hierarchy(ch, target, table=table, min_free_bytes=min_free_gib << 30)))
    finally:
        ch.close()


@main.command("ch-narrow-coarse-bench")
@option("-a", "--contains", is_flag=True, help="Interpret --name as a basename substring (benchmark only; refuses matching directories)")
@option("-b", "--before", "date0", help="Also serve and independently verify a two-date coarse diff")
@option("-c", "--compare", is_flag=True, help="Verify complete heavy-child sets and parent totals against an independent posting scan")
@option("-C", "--cold", is_flag=True, help="Reset data caches independently before optimized/oracle reads; prefix remains resident")
@option("-d", "--date", required=True, help="One completed frozen historical scan")
@option("-k", "--child-budget", default=64, type=IntRange(min=1, max=256), help="Maximum byte-quantile positions per view")
@option("-l", "--levels", default=1, type=IntRange(min=1, max=4), help="Experimental batched refinement at a fixed global threshold")
@option("-m", "--materialize", is_flag=True, help="Build a session-only preorder disk cache for a suffix experiment")
@option("-n", "--name", required=True, help="One exact leaf basename")
@option("-o", "--out", type=Path, help="Write private view bodies to this scratch directory")
@option("-p", "--path", "paths", multiple=True, help="Explicit view path; otherwise root plus two large directory drills")
@option("-r", "--prepared-set", is_flag=True, help="Benchmark a reusable session Set engine for vocabulary membership")
@option("-s", "--suffix", is_flag=True, help="Interpret --name as a basename suffix (benchmark only; no serving change)")
@option("-t", "--threads", default=8, type=IntRange(min=1), help="Equal query thread limits")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_coarse_bench(
    contains: bool,
    date0: str | None,
    compare: bool,
    cold: bool,
    date: str,
    child_budget: int,
    levels: int,
    materialize: bool,
    name: str,
    out: Path | None,
    paths: tuple[str, ...],
    prepared_set: bool,
    suffix: bool,
    threads: int,
    url: str,
    target: str,
) -> None:
    """Exact coarse-directory prototype; no normal serving changes."""
    from .chstore.coarse import bench

    print(json.dumps(bench(url, target, date, name, budget=child_budget, cold=cold, compare=compare, paths=paths, out=out, threads=threads, date0=date0, suffix=suffix, contains=contains, materialize=materialize, levels=levels, prepared_set=prepared_set)))


@main.command("ch-hot-name-scopes")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-n", "--name", required=True, help="Exact basename whose first eight postings provide possible benchmark scopes")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_name_scopes(
    date: str,
    name: str,
    url: str,
    target: str,
) -> None:
    """Find bounded complete parents; not a representative corpus sample."""
    from .chstore.hot_names import find_scopes

    print(json.dumps(find_scopes(url, target, date, name)))


@main.command("ch-hot-name-hash-bench")
@option("-l", "--leaves", default=8192, type=IntRange(min=1, max=16_000), help="Deterministic hash-named leaves in a complete synthetic tree")
@option("-n", "--pattern", multiple=True, required=True, help="Name literal <=7 chars")
@option("-t", "--threshold", multiple=True, required=True, type=IntRange(min=1), help="Minimum direct-matching paths to materialize")
def ch_hot_name_hash_bench(
    leaves: int,
    pattern: tuple[str, ...],
    threshold: tuple[int, ...],
) -> None:
    """Bounded synthetic hash workload; run on the dev node."""
    from .chstore.hot_names import bench_nodes, hash_fixture

    print(json.dumps({"scope": "complete deterministic synthetic hash tree; not fleet acceptance", **bench_nodes(hash_fixture(leaves), threshold, pattern)}))


@main.command("ch-hot-name-bench")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-n", "--pattern", multiple=True, required=True, help="One name-only literal <=7 chars; repeat for several queries")
@option("-p", "--path", required=True, help="Complete subtree with <=20K union nodes")
@option("-t", "--threshold", multiple=True, required=True, type=IntRange(min=1), help="Materialize substrings matching at least this many local paths")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_name_bench(
    date: str,
    pattern: tuple[str, ...],
    path: str,
    threshold: tuple[int, ...],
    url: str,
    target: str,
) -> None:
    """Bounded hot-substring aggregates with exact three-level partition checks."""
    from .chstore.hot_names import bench

    print(json.dumps(bench(url, target, date, path, threshold, pattern)))


@main.command("ch-hot-l1-bench")
@option("-d", "--date", required=True, help="Frozen global scan date")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query memory budget")
@option("-n", "--pattern", required=True, help="One case-insensitive name substring without slashes")
@option("-N", "--max-names", type=IntRange(min=1), help="Optional matching-vocabulary guard; refuse rather than truncate")
@option("-o", "--out", required=True, type=Path, help="New private dev-node JSON artifact; never overwrites")
@option("-p", "--rss-pid", multiple=True, type=IntRange(min=1), help="Host process RSS to monitor; output beside artifact")
@option("-P", "--max-postings", type=IntRange(min=1), help="Optional direct dated node guard before directory sorting; no descendant expansion")
@option("-r", "--reference-batch", type=Path, help="Explicit trusted completed native batch artifact; replaces independent full-source oracle")
@option("-R", "--max-roots", type=IntRange(min=1), help="Optional deduplicated outer-directory guard; refuse rather than truncate")
@option("-s", "--spill-gib", default=8, type=IntRange(min=1, max=16), help="Offline temporary-disk budget per query")
@option("-w", "--timeout-seconds", default=300, type=IntRange(min=1, max=600), help="Per-statement offline deadline; throws, not partial")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_l1_bench(
    date: str,
    memory_gib: int,
    pattern: str,
    max_names: int | None,
    out: Path,
    rss_pid: tuple[int, ...],
    max_postings: int | None,
    reference_batch: Path | None,
    max_roots: int | None,
    spill_gib: int,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Exact global L1 hot-query aggregate; offline prototype, not serving."""
    from .chstore.hot_l1_bench import bench

    print(json.dumps(bench(url, target, date, pattern, out, memory_gib=memory_gib, seconds=timeout_seconds,
                           spill_gib=spill_gib, pids=rss_pid, reference_batch=reference_batch,
                           **{key: value for key, value in (('max_names', max_names), ('max_postings', max_postings), ('max_roots', max_roots)) if value is not None})))


@main.command("ch-hot-l1-batch-bench")
@option("-b", "--binary", type=Path, help="Explicit precompiled native executable; requires --engine stream")
@option("-d", "--date", required=True, help="Frozen global scan date")
@option("-e", "--engine", type=Choice(["sql", "stream"]), default="sql", help="Batch construction engine; stream requires --binary")
@option("-g", "--registry-date", help="Explicit source date of a reused hot-query registry; does not imply historical threshold coverage")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query memory budget")
@option("-o", "--out", required=True, type=Path, help="New private batch L1 JSON artifact; never overwrites")
@option("-p", "--rss-pid", multiple=True, type=IntRange(min=1), help="Host process RSS to monitor; output beside artifact")
@option("-q", "--queries", required=True, type=Path, help="Completed matching-date hot-query JSONL export")
@option("-r", "--reference", multiple=True, type=Path, help="Independently checked same-date single-query L1 artifact")
@option("-s", "--spill-gib", default=8, type=IntRange(min=1, max=16), help="Offline temporary-disk budget per query")
@option("-w", "--timeout-seconds", default=600, type=IntRange(min=1, max=3600), help="Offline per-statement deadline; errors, not partial results")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_l1_batch_bench(
    binary: Path | None,
    date: str,
    engine: str,
    registry_date: str | None,
    memory_gib: int,
    out: Path,
    rss_pid: tuple[int, ...],
    queries: Path,
    reference: tuple[Path, ...],
    spill_gib: int,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Batch all registered hot predicates into exact L1 scalar summaries."""
    from .chstore.hot_l1_batch_bench import bench

    print(json.dumps(bench(url, target, date, queries, out, memory_gib=memory_gib,
                           seconds=timeout_seconds, spill_gib=spill_gib, pids=rss_pid, references=reference,
                           registry_date=registry_date, engine=engine, binary=binary)))


@main.command("ch-hot-l1-read")
@option("-d", "--date", required=True, help="Registered scan date")
@option("-D", "--compare-from", help="Registered earlier scan date for an exact signed diff")
@option("-n", "--pattern", required=True, help="Registered case-insensitive name substring")
@option("-p", "--path", default="", help="Root only; unmaterialized drills are explicitly refused")
@argument("artifacts", nargs=-1, required=True, type=Path)
def ch_hot_l1_read(
    date: str,
    compare_from: str | None,
    pattern: str,
    path: str,
    artifacts: tuple[Path, ...],
) -> None:
    """Read exact L1 summaries/diffs without ClickHouse or a scan fallback."""
    from .chstore.hot_l1_catalog import HotL1Catalog

    catalog = HotL1Catalog.load(artifacts)
    body = catalog.diff(compare_from, date, pattern, path=path) if compare_from else catalog.view(date, pattern, path=path)
    print(json.dumps(body))


@main.command("ch-hot-l1-prefix-audit")
@option("-a", "--accepted", required=True, type=Path, help="Accepted complete batch artifact to bind snapshot identity and node count")
@option("-b", "--binary", required=True, type=Path, help="Native engine with --prefix-audit support")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-o", "--out", required=True, type=Path, help="New private completed prefix-closure proof JSON")
@option("-w", "--timeout-seconds", default=1800, type=IntRange(min=1, max=3600), help="Offline per-statement deadline")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_l1_prefix_audit(
    accepted: Path,
    binary: Path,
    date: str,
    out: Path,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Prove per-date immediate-parent presence without rebuilding predicates."""
    from .chstore.hot_l1_prefix_audit import bench

    print(json.dumps(bench(url, target, date, accepted, out, binary=binary, seconds=timeout_seconds)))


@main.command("ch-hot-l1-batch-read")
@option("-d", "--date", required=True, help="Registered scan date")
@option("-D", "--compare-from", help="Registered earlier scan date for an exact signed diff")
@option("-n", "--pattern", required=True, help="Registered case-insensitive name substring")
@option("-p", "--path", default="", help="Root only; unmaterialized drills are explicitly refused")
@argument("artifacts", nargs=-1, required=True, type=Path)
def ch_hot_l1_batch_read(
    date: str,
    compare_from: str | None,
    pattern: str,
    path: str,
    artifacts: tuple[Path, ...],
) -> None:
    """Read registered SQL/native L1 batch summaries without a scan fallback."""
    from .chstore.hot_l1_batch_catalog import HotL1BatchCatalog

    catalog = HotL1BatchCatalog.load(artifacts)
    body = catalog.diff(compare_from, date, pattern, path=path) if compare_from else catalog.view(date, pattern, path=path)
    print(json.dumps(body))


@main.command("ch-hot-l1-publish")
@option("-o", "--root", required=True, type=Path, help="Explicit private local catalog root; atomically publishes current.json")
@option("-p", "--prefix-proof", multiple=True, type=Path, help="Optional complete prefix proofs; when supplied must cover every published scan")
@argument("artifacts", nargs=-1, required=True, type=Path)
def ch_hot_l1_publish(
    root: Path,
    prefix_proof: tuple[Path, ...],
    artifacts: tuple[Path, ...],
) -> None:
    """Publish completed referenced batch artifacts as one immutable generation."""
    from .chstore.hot_l1_publish import publish

    print(json.dumps(publish(artifacts, root, prefix_proofs=prefix_proof)))


@main.command("ch-hot-l1-http-bench")
@option("-a", "--all-registered", is_flag=True, help="Verify the requested scan's entire registered predicate catalog; mutually exclusive with --pattern")
@option("-d", "--date", required=True, help="Registered requested scan date")
@option("-D", "--compare-from", help="Registered earlier scan date for complete diff-body acceptance")
@option("-g", "--generation-root", required=True, type=Path, help="Published private local catalog root to pin once")
@option("-n", "--pattern", multiple=True, help="Explicit registered literal; repeatable, mutually exclusive with --all-registered")
@option("-o", "--out", required=True, type=Path, help="New compact HTTP benchmark JSON; never overwrites")
@option("-t", "--trials", default=3, type=IntRange(min=1), help="Verified responses per registered query")
@option("-T", "--token-env", required=True, help="Environment variable containing bearer token; token is never logged")
@option("-w", "--timeout-seconds", default=30, type=IntRange(min=1), help="HTTP request timeout in seconds")
@option("-U", "--url", required=True, help="Explicit standalone hot L1 HTTP base URL")
def ch_hot_l1_http_bench(
    all_registered: bool,
    date: str,
    compare_from: str | None,
    generation_root: Path,
    pattern: tuple[str, ...],
    out: Path,
    trials: int,
    token_env: str,
    timeout_seconds: int,
    url: str,
) -> None:
    """Verify every complete registered HTTP response against a pinned reader."""
    from .chstore.hot_l1_http_bench import bench

    if bool(pattern) == all_registered:
        raise UsageError("HTTP benchmark requires either --pattern or --all-registered, not both")
    print(json.dumps(bench(generation_root, url, date, pattern, out, token_env=token_env,
                           compare_from=compare_from, trials=trials, timeout=timeout_seconds, all_registered=all_registered)))


@main.command("serve-hot-l1")
@option("-A", "--no-auth", is_flag=True, help="Serve without bearer authentication; numeric loopback bind only")
@option("-b", "--bind", default="127.0.0.1", help="Address to listen on; isolated from normal query serving")
@option("-g", "--generation-root", type=Path, help="Published local generation root to pin once; mutually exclusive with artifact paths")
@option("-p", "--port", default=8082, type=IntRange(min=1, max=65535), help="Standalone dev HTTP port")
@option("-T", "--token-env", default="QUERY_BOX_TOKEN", help="Environment variable containing the required bearer token")
@argument("artifacts", nargs=-1, type=Path)
def serve_hot_l1(
    no_auth: bool,
    bind: str,
    generation_root: Path | None,
    port: int,
    token_env: str,
    artifacts: tuple[Path, ...],
) -> None:
    """Serve registered root L1 batch queries/diffs without ClickHouse."""
    from .chstore.hot_l1_http import serve, token_from_env

    if bool(artifacts) == (generation_root is not None):
        raise UsageError("hot L1 serving requires either explicit artifacts or generation_root, not both")
    token = None if no_auth else token_from_env(token_env)
    if token is None and not no_auth:
        raise UsageError(f"${token_env} is unset (or pass -A for loopback-only no-auth serving)")
    if no_auth:
        from .chstore.hot_l1_http import _loopback

        if not _loopback(bind):
            raise UsageError("hot L1 no-auth serving requires a numeric loopback bind")
    serve(artifacts, generation_root=generation_root, bind=bind, port=port, token=token)


@main.command("ch-hot-frequency-union")
@option("-c", "--max-patterns", default=500_000, type=IntRange(min=1, max=500_000), help="Complete union pattern cap; exceeding it refuses before writing")
@option("-k", "--max-chars", required=True, type=IntRange(min=0, max=32), help="Requested complete union depth; every source must cover it (0 = the complete length domain: every source a complete census)")
@option("-o", "--out", required=True, type=Path, help="Fresh private complete union JSONL artifact; never overwrites")
@option("-s", "--source", multiple=True, required=True, type=(Path, Path), help="Accepted single-date CENSUS QUERIES pair; repeat for distinct dates (one pair = that scan's own registry)")
@option("-t", "--threshold", required=True, type=IntRange(min=1), help="Hot if any source date qualifies; cannot be below any source census minimum")
@option("-S", "--short-chars", default=0, type=IntRange(min=0, max=32), help="Also register every literal of at most this many characters the sources list (their short-literal domains must cover it), whatever its frequency")
@option("-T", "--target", help="Explicit logical registry binding for sources from different physical stores; each source then declares its own target")
def ch_hot_frequency_union(
    max_patterns: int,
    max_chars: int,
    out: Path,
    source: tuple[tuple[Path, Path], ...],
    threshold: int,
    short_chars: int,
    target: str | None,
) -> None:
    """Union dated exact registries without claiming every query is hot on each scan."""
    from .chstore.hot_frequency_union import union

    print(json.dumps(union(source, threshold, max_chars or None, out, max_patterns=max_patterns, short_chars=short_chars,
                           **({} if target is None else {"target": target}))))


@main.command("ch-hot-frequency-equivalence")
@option("-o", "--out", required=True, type=Path, help="Fresh private full alias-proof report; no registry or kernel mutation")
@option("-s", "--expected-sha256", help="Expected complete union-export SHA256")
@argument("queries", type=Path)
def ch_hot_frequency_equivalence(
    out: Path,
    expected_sha256: str | None,
    queries: Path,
) -> None:
    """Size exact date-bound substring aliases in a completed union registry."""
    from .chstore.hot_frequency_equivalence import report

    body = report(queries, out, expected_sha256=expected_sha256)
    savings = [{'date': row['date'], **{side + '_percent': None if bound is None else round(100 * bound['numerator'] / bound['denominator'], 6)
                                      for side, bound in row['removed_fraction_bounds'].items()}} for row in body['direct_hit_work']]
    print(json.dumps({**{key: body[key] for key in ('schema', 'dates', 'patterns', 'classes', 'aliases_removed', 'nontrivial_classes', 'accepted_for_kernel')},
                      'direct_hit_work_savings_percent_bounds': savings, 'out': str(out)}))


@main.command("ch-hot-frequency-compare")
@argument("census_a", type=Path)
@argument("queries_a", type=Path)
@argument("census_b", type=Path)
@argument("queries_b", type=Path)
def ch_hot_frequency_compare(
    census_a: Path,
    queries_a: Path,
    census_b: Path,
    queries_b: Path,
) -> None:
    """Compare all exact literal frequencies in two controls' common T/L domain."""
    from .chstore.hot_frequency_report import compare

    print(json.dumps(compare(census_a, queries_a, census_b, queries_b)))


@main.command("ch-hot-frequency-report")
@option("-k", "--max-chars", multiple=True, type=IntRange(min=0, max=32), help="Grid depth; repeat (default 7,12,16), never above the completed census depth (0 = every length, from a complete census)")
@option("-n", "--pattern", multiple=True, help="Literal frequency to report; repeat (default .json,zarr.json,.npy)")
@option("-t", "--threshold", multiple=True, type=IntRange(min=1), help="Grid minimum matching paths; repeat (default 100k,300k,1M), never below source threshold")
@argument("census", type=Path)
@argument("queries", type=Path)
def ch_hot_frequency_report(
    max_chars: tuple[int, ...],
    pattern: tuple[str, ...],
    threshold: tuple[int, ...],
    census: Path,
    queries: Path,
) -> None:
    """Validate local complete artifacts and report exact T/L catalog sizing."""
    from .chstore.hot_frequency_report import report

    print(json.dumps(report(census, queries, thresholds=threshold or (100_000, 300_000, 1_000_000),
                            lengths=tuple(chars or None for chars in max_chars) or (7, 12, 16), patterns=pattern or (".json", "zarr.json", ".npy"))))


@main.command("ch-mega-names")
@option("-d", "--date", "dates", multiple=True, required=True, help="Published scan date in the consolidated store; repeat")
@option("-D", "--db", default="default", help="Consolidated store database")
@option("-n", "--pattern", "patterns", multiple=True, required=True, help="Slash-free literal; repeat")
@option("-P", "--postings", help="Name-sorted postings stem (`ch-mega-names-build`); default: the store's own `by_name` projections")
@option("-r", "--reference", multiple=True, type=(str, str), help="DATE TARGET to compare against: a frozen snapshot target, or `daily:TARGET` for a daily scalar target's own name index; repeat")
@option("-t", "--threads", default=8, type=IntRange(min=1, max=64), help="ClickHouse max_threads per statement")
@option("-T", "--trials", default=1, type=IntRange(min=1, max=10), help="Runs per (date, literal); the first is the coldest")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
def ch_mega_names(
    dates: tuple[str, ...],
    db: str,
    patterns: tuple[str, ...],
    postings: str | None,
    reference: tuple[tuple[str, str], ...],
    threads: int,
    trials: int,
    url: str,
) -> None:
    """Name-substring bucket totals for any published scan from the consolidated
    store (`nodes`/`closures` `by_name` projections, `names` vocabulary): one
    JSON record per (date, literal) with stage timings, optionally checked
    against a per-scan or frozen index's answer."""
    from .chstore import mega_names

    refs = {d: (t.removeprefix("daily:"), t.startswith("daily:")) for d, t in reference}
    for record in mega_names.bench(url, db, list(dates), list(patterns), threads=threads, trials=trials, references=refs, postings=postings):
        print(json.dumps(record), flush=True)


@main.command("ch-mega-names-build")
@option("-D", "--db", default="default", help="Consolidated store database")
@option("-m", "--memory-gib", default=64, type=IntRange(min=1, max=200), help="Per-statement memory cap")
@option("-s", "--start", help="Span start (scan date): keep only versions live on or after it; default all time")
@option("-S", "--spans", is_flag=True, help="Also (re)build `name_spans`")
@option("-t", "--threads", default=32, type=IntRange(min=1, max=64), help="ClickHouse max_threads")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("stem", required=False)
def ch_mega_names_build(db: str, memory_gib: int, start: str | None, spans: bool, threads: int, url: str, stem: str | None) -> None:
    """Build the consolidated store's name index: STEM's name-sorted postings
    (`{STEM}_nodes`, `{STEM}_closures`; `-s` limits them to a span), and/or the
    `name_spans` vocabulary filter (`-S`). Prints sizes and timings as JSON."""
    from .chstore import mega_names
    from .chstore.client import Ch

    settings = {"max_threads": threads, "max_insert_threads": threads, "max_memory_usage": memory_gib << 30,
                "max_bytes_before_external_group_by": memory_gib << 29, "max_bytes_before_external_sort": memory_gib << 29,
                "join_algorithm": "full_sorting_merge"}
    ch = Ch(url, db=db, timeout=7200)
    try:
        if spans:
            print(json.dumps({"name_spans": mega_names.build_spans(ch, settings)}), flush=True)
        if stem:
            print(json.dumps({"postings": mega_names.build_postings(ch, stem, start, settings)}), flush=True)
    finally:
        ch.close()


@main.command("ch-daily-name-index")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=24), help="Per-statement memory budget")
@option("-s", "--spill-gib", default=4, type=IntRange(min=1, max=16), help="External GROUP BY / sort threshold per statement")
@option("-w", "--timeout-seconds", default=3600, type=IntRange(min=1, max=7200), help="Per-statement deadline; throws, never marks a partial index complete")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_daily_name_index(
    memory_gib: int,
    spill_gib: int,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Add a completed daily scalar TARGET's own bounded name postings
    (`names`, `nodes_by_name`, `name_index_manifest`): the cold fallback
    `serve-query -C` uses for that scan's unregistered literals. Fresh
    construction only; prints the completion manifest."""
    from .chstore import daily_name_index
    from .chstore.client import Ch

    ch = Ch(url, timeout=timeout_seconds + 60)
    try:
        body = daily_name_index.build(ch, target, memory_bytes=memory_gib << 30, spill_bytes=spill_gib << 30, query_seconds=timeout_seconds)
    finally:
        ch.close()
    print(json.dumps(body, indent=2))


@main.command("ch-hot-frequency-census")
@option("-b", "--staging-gib", default=16, type=IntRange(min=1, max=32), help="Daily-source active temporary-table coexistence cap; checked at stage boundaries")
@option("-c", "--max-patterns", default=500_000, type=IntRange(min=1), help="Cumulative accepted hot-pattern cap; exceeding it fails without a complete artifact")
@option("-d", "--date", required=True, help="Frozen global scan date")
@option("-e", "--native", type=Path, help="Run the per-length passes in this `native/hot_frequency.cpp` binary (with `-f`): one streamed GROUP BY, no staged tables")
@option("-f", "--daily-source", type=Path, help="Explicit accepted global daily scalar source manifest instead of frozen history/name IDs")
@option("-h", "--threshold-cut", multiple=True, type=IntRange(min=1), help="Additional direct-path threshold to count from the same minimum-threshold census")
@option("-k", "--max-chars", default=7, type=IntRange(min=0, max=32), help="Enumerate threshold-hot name substrings up to this length (maximum 32 characters); 0 = every length until none is hot (the complete domain; needs `-e`)")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query memory budget")
@option("-n", "--pattern", multiple=True, help="Selected literals to report if threshold-hot and within max length")
@option("-o", "--out", required=True, type=Path, help="New private dev-node JSON census artifact; never overwrites")
@option("-p", "--rss-pid", multiple=True, type=IntRange(min=1), help="Host process RSS to monitor; output beside artifact")
@option("-q", "--queries-out", type=Path, help="New private JSONL hot-predicate export with mandatory completion footer")
@option("-s", "--spill-gib", default=8, type=IntRange(min=1, max=16), help="Offline temporary-disk budget per query")
@option("-S", "--short-chars", default=0, type=IntRange(min=0, max=32), help="Also list every literal of at most this many characters present (≥ 1 path), whatever its frequency: the short-literal domain, precomputed by cost (needs `-e`)")
@option("-t", "--threshold", required=True, type=IntRange(min=1), help="Minimum direct matching paths; not occurrences or bytes")
@option("-v", "--wall-seconds", default=3600, type=IntRange(min=1, max=7200), help="Daily-source nonrenewable total census deadline, plus at most sixty cleanup seconds")
@option("-w", "--timeout-seconds", default=600, type=IntRange(min=1, max=600), help="Per-statement offline deadline; throws, not partial")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_frequency_census(
    staging_gib: int,
    max_patterns: int,
    date: str,
    native: Path | None,
    daily_source: Path | None,
    threshold_cut: tuple[int, ...],
    max_chars: int,
    memory_gib: int,
    pattern: tuple[str, ...],
    out: Path,
    rss_pid: tuple[int, ...],
    queries_out: Path | None,
    spill_gib: int,
    short_chars: int,
    threshold: int,
    wall_seconds: int,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Exact full-fleet threshold-hot substring counts, pruning cold prefixes."""
    from .chstore.hot_frequency_bench import bench

    source = {} if daily_source is None else {'daily_source': daily_source, 'wall_seconds': wall_seconds, 'staging_gib': staging_gib, 'native': native,
                                              'short_chars': short_chars}
    if short_chars and daily_source is None:
        raise UsageError('the short-literal domain (`-S`) needs the native engine (`-f`, `-e`)')
    print(json.dumps(bench(url, target, date, threshold, max_chars or None, out, memory_gib=memory_gib,
                           seconds=timeout_seconds, spill_gib=spill_gib, pids=rss_pid, patterns=pattern,
                           queries_out=queries_out, thresholds=threshold_cut, max_patterns=max_patterns, **source)))


@main.command("ch-hot-l2-pair-bench")
@option("-a", "--all-registered", is_flag=True, help="Explicitly attempt all registered predicates; output guards may refuse")
@option("-b", "--binary", required=True, type=Path, help="Explicit native paired L2 executable")
@option("-c", "--budget", default=64, type=IntRange(min=1, max=4096), help="Heavy-child budget per query and bucket root")
@option("-C", "--source-client", type=Path, help="Explicit immutable multicall ClickHouse binary for loopback TCP sources; no ambient credentials/config")
@option("-d", "--date", required=True, help="After scan date")
@option("-D", "--before-date", required=True, help="Earlier scan date")
@option("-e", "--max-cells", default=10_000_000, type=IntRange(min=1, max=10_000_000), help="Native sparse-output hard cap, not a fit guarantee")
@option("-f", "--max-frames", default=500_000, type=IntRange(min=1, max=500_000), help="Complete depth-2 union frame guard")
@option("-l", "--stdout-mib", default=512, type=IntRange(min=1, max=512), help="Native stdout byte cap")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Per-source CH query memory budget; two concurrent sources")
@option("-n", "--pattern", multiple=True, help="Registered literal; repeat, or use --all-registered")
@option("-o", "--out", required=True, type=Path, help="New private experiment artifact, never a catalog publication")
@option("-p", "--prefix-proof", multiple=True, required=True, type=Path, help="Artifact-bound proof; exactly one for each date")
@option("-q", "--queries", required=True, type=Path, help="Same completed query export as both accepted references")
@option("-r", "--reference", multiple=True, required=True, type=Path, help="Accepted native L1 artifact; exactly one for each date")
@option("-s", "--spill-gib", default=8, type=IntRange(min=1, max=8), help="Per-source temporary-disk budget")
@option("-w", "--timeout-seconds", default=3600, type=IntRange(min=1, max=3600), help="Per-source statement deadline; never partial")
@option("-W", "--wall-seconds", default=4500, type=IntRange(min=1, max=4500), help="Native experiment wall guard plus bounded cleanup")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_l2_pair_bench(
    all_registered: bool,
    binary: Path,
    budget: int,
    source_client: Path | None,
    date: str,
    before_date: str,
    max_cells: int,
    max_frames: int,
    stdout_mib: int,
    memory_gib: int,
    pattern: tuple[str, ...],
    out: Path,
    prefix_proof: tuple[Path, ...],
    queries: Path,
    reference: tuple[Path, ...],
    spill_gib: int,
    timeout_seconds: int,
    wall_seconds: int,
    url: str,
    target: str,
) -> None:
    """Offline paired sparse L2 experiment; fixed per-bucket thresholds only."""
    from .chstore.hot_l2_pair_stream import bench

    body = bench(url, target, before_date, date, reference, prefix_proof, queries, out,
                 binary=binary, patterns=pattern, all_registered=all_registered, budget=budget,
                 memory_gib=memory_gib, spill_gib=spill_gib, seconds=timeout_seconds,
                 wall_seconds=wall_seconds, max_frames=max_frames, max_cells=max_cells,
                 max_output_bytes=stdout_mib << 20,
                 **({'source_client': source_client} if source_client is not None else {}))
    print(json.dumps({key: body[key] for key in ('schema', 'dates', 'registered_predicates', 'registered_frames', 'query_subset', 'timings')} | {'out': str(out), 'cells': len(body['cells'])}))


@main.command("ch-hot-registry-select")
@option("-i", "--source-manifest", required=True, type=Path, help="Completed global daily scalar source manifest to pin")
@option("-l", "--logical-store", required=True, help="Explicit logical store binding, distinct from the registry's physical target")
@option("-o", "--out", required=True, type=Path, help="Fresh private mode-0600 selection envelope; never overwrites")
@option("-r", "--registry", required=True, type=Path, help="Original complete dated-union registry JSONL; read only, never rewritten")
def ch_hot_registry_select(
    source_manifest: Path,
    logical_store: str,
    out: Path,
    registry: Path,
) -> None:
    """Pin unchanged union membership to an explicitly accepted daily source."""
    from hashlib import sha256
    from .chstore.daily_scalar import manifest_bytes
    from .chstore.hot_registry_selection import DOCUMENT_LIMIT, REGISTRY_LIMIT, envelope

    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError("registry selection output must be fresh with an existing parent directory")
    with registry.open("rb") as source:
        registry_raw = source.read(REGISTRY_LIMIT + 1)
    with source_manifest.open("rb") as source:
        source_raw = source.read(DOCUMENT_LIMIT + 1)
    body = envelope(registry_raw, source_raw, logical_store=logical_store)
    raw = manifest_bytes(body)
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            os.fchmod(output.fileno(), 0o600)
            output.write(raw)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        out.unlink()
        raise
    print(json.dumps({"schema": body["schema"], "date": body["build_source"]["date"], "patterns": body["registry"]["patterns"],
                      "selection_sha256": sha256(raw).hexdigest(), "selection_bytes": len(raw)}))


@main.command("ch-dated-hot-l1-publish")
@option("-a", "--artifact", multiple=True, required=True, type=Path, help="Complete private dated native L1 artifact; repeat for each scan")
@option("-b", "--bucket-path", multiple=True, required=True, help="Explicit complete bucket path scope; repeat for each bucket")
@option("-l", "--logical-store", required=True, help="Explicit logical store shared by all dated artifacts")
@option("-p", "--proof", multiple=True, required=True, type=Path, help="Artifact-bound selected full-source proof; repeat for each scan")
@argument("root", type=Path)
def ch_dated_hot_l1_publish(
    artifact: tuple[Path, ...],
    bucket_path: tuple[str, ...],
    logical_store: str,
    proof: tuple[Path, ...],
    root: Path,
) -> None:
    """Atomically publish accepted dated L1 files; no routing or deployment."""
    from .chstore.dated_hot_l1_publish import publish

    body = publish(artifact, root, proofs=proof, logical_store=logical_store, bucket_paths=bucket_path)
    print(json.dumps({"schema": body["schema"], "generation": body["generation"], "dates": body["dates"],
                      "artifacts": len(body["artifacts"]), "artifact_bytes": sum(row["bytes"] for row in body["artifacts"]),
                      "proof_bytes": sum(row["bytes"] for row in body["proofs"])}))


@main.command("ch-dated-hot-l1-build")
@option("-b", "--binary", required=True, type=Path, help="Explicit native L1 executable to pin by SHA256")
@option("-i", "--source-manifest", required=True, type=Path, help="Pinned completed global daily scalar source manifest")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-statement memory cap")
@option("-o", "--out", required=True, type=Path, help="Fresh private dated L1 artifact; never overwrites")
@option("-r", "--registry", required=True, type=Path, help="Original completed dated-union registry JSONL; never rewritten")
@option("-s", "--selection", required=True, type=Path, help="Explicit registry-to-daily-source selection envelope")
@option("-t", "--timeout-seconds", default=1800, type=IntRange(min=1, max=3600), help="Per-statement deadline, not an overall build SLA")
@option("-T", "--spill-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query simultaneous temporary disk cap")
@option("-U", "--url", default="http://127.0.0.1:8123", help="Existing development ClickHouse HTTP endpoint")
def ch_dated_hot_l1_build(
    binary: Path,
    source_manifest: Path,
    memory_gib: int,
    out: Path,
    registry: Path,
    selection: Path,
    timeout_seconds: int,
    spill_gib: int,
    url: str,
) -> None:
    """Build root-only dated L1 with unchanged registry qualification; no publication."""
    from hashlib import sha256
    from .chstore.client import Ch
    from .chstore.dated_hot_l1 import build
    from .chstore.hot_registry_selection import load

    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError("dated L1 output must be fresh with an existing parent directory")
    pinned = load(selection, registry, source_manifest)
    ch = Ch(url, timeout=timeout_seconds + 60, max_threads=4, max_memory_usage=memory_gib << 30,
            max_temporary_data_on_disk_size_for_query=spill_gib << 30,
            max_execution_time=timeout_seconds, timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    try:
        body = build(ch, pinned, binary=binary, out=out)
        digest, size = sha256(), 0
        with out.open("rb") as artifact:
            while chunk := artifact.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
        print(json.dumps({"schema": body["schema"], "date": body["date"], "patterns": len(pinned.patterns),
                          "aliases": len(body.get("aliases", [])), "artifact_bytes": size, "artifact_sha256": digest.hexdigest(), "stages": body["stages"]}))
    finally:
        ch.close()


@main.command("ch-dated-hot-l1-check")
@option("-m", "--memory-gib", default=4, type=IntRange(min=1, max=8), help="Per-statement oracle memory cap")
@option("-n", "--pattern", multiple=True, required=True, help="Registered literal to check independently; repeat, one to eight unique names")
@option("-o", "--out", required=True, type=Path, help="Fresh private mode-0600 selected-source proof; never overwrites")
@option("-t", "--timeout-seconds", default=600, type=IntRange(min=1, max=600), help="Per-statement oracle deadline, not an overall SLA")
@option("-U", "--url", default="http://127.0.0.1:8123", help="Development ClickHouse HTTP endpoint")
@argument("artifact", type=Path)
def ch_dated_hot_l1_check(
    memory_gib: int,
    pattern: tuple[str, ...],
    out: Path,
    timeout_seconds: int,
    url: str,
    artifact: Path,
) -> None:
    """Check selected dated L1 roots against complete independent source scans."""
    from hashlib import sha256
    from math import isfinite
    from .chstore.client import Ch
    from .chstore.daily_scalar import manifest_bytes
    from .chstore.dated_hot_l1 import DatedHotL1Catalog
    from .chstore.dated_hot_l1_check import check
    from .chstore.hot_l1_batch_catalog import _literal

    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError("dated L1 check output must be fresh with an existing parent directory")
    patterns = tuple(_literal(value) for value in pattern)
    if not 1 <= len(patterns) <= 8 or len(set(patterns)) != len(patterns):
        raise ValueError("dated L1 check requires one to eight unique registered literals")
    catalog = DatedHotL1Catalog.load(artifact)
    registered = frozenset(catalog.selection.patterns)
    if any(value not in registered for value in patterns):
        raise ValueError("dated L1 check requires one to eight unique registered literals")
    ch = Ch(url, db=catalog.selection.snapshot_db, timeout=timeout_seconds + 60, max_threads=1,
            max_memory_usage=memory_gib << 30, max_execution_time=timeout_seconds,
            timeout_before_checking_execution_speed=0, timeout_overflow_mode="throw")
    try:
        body = check(ch, catalog, patterns)
        if (body.get("schema") != "dated-hot-l1-check-v1" or body.get("complete") is not True or
                body.get("date") != catalog.date or type(body.get("source_nodes")) is not int or body["source_nodes"] != catalog.selection.nodes or
                type(body.get("selected_patterns_checked")) is not int or body["selected_patterns_checked"] != len(patterns) or
                type(body.get("check_s")) not in (int, float) or not isfinite(body["check_s"]) or body["check_s"] < 0):
            raise ValueError("dated L1 checker did not return a complete matching selected-source proof")
        raw = manifest_bytes(body)
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                os.fchmod(output.fileno(), 0o600)
                output.write(raw)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            out.unlink()
            raise
        print(json.dumps({key: body[key] for key in ("schema", "date", "source_nodes", "selected_patterns_checked", "check_s")} |
                         {"proof_sha256": sha256(raw).hexdigest(), "proof_bytes": len(raw)}))
    finally:
        ch.close()


@main.command("ch-daily-scalar-build")
@option("-b", "--owned-gib", default=35, type=IntRange(min=1, max=35), help="Owned table budget checked at stage boundaries, not an in-flight disk quota")
@option("-e", "--order-plan", default="window", type=Choice(["window", "physical"]), help="Explicit ID ordering plan; physical uses bounded tree-sorted staging and audited serial numbering")
@option("-f", "--reserve-gib", default=20, type=IntRange(min=20), help="Minimum free disk before and after each construction stage")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Per-statement memory cap")
@option("-n", "--max-nodes", default=1000000, type=IntRange(min=1, max=(1 << 32) - 1), help="Complete selected subtree node cap; never samples")
@option("-o", "--out", required=True, type=Path, help="Fresh private accepted-source manifest output")
@option("-p", "--prefix", default="", help="Exact complete subtree; empty means the global fleet")
@option("-r", "--resume-evidence", type=Path, help="Explicit private query-log provenance for a failed raw/scalar-only global build; never automatic")
@option("-s", "--source-descriptor", required=True, type=Path, help="Pinned local input length/SHA256/date/store/generation descriptor")
@option("-S", "--sort-spill-mib", default=256, type=IntRange(min=1, max=2048), help="External-sort run threshold within the unchanged memory cap (at most one quarter)")
@option("-t", "--timeout-seconds", default=1800, type=IntRange(min=1, max=3600), help="Per-statement execution deadline, not an overall build SLA")
@option("-T", "--spill-gib", default=8, type=IntRange(min=1, max=8), help="Per-query simultaneous temporary disk cap")
@option("-U", "--url", default="http://127.0.0.1:8123", help="Existing development ClickHouse HTTP endpoint")
@argument("target")
@argument("parquet", type=Path)
def ch_daily_scalar_build(
    owned_gib: int,
    order_plan: str,
    reserve_gib: int,
    memory_gib: int,
    max_nodes: int,
    out: Path,
    prefix: str,
    resume_evidence: Path | None,
    source_descriptor: Path,
    sort_spill_mib: int,
    timeout_seconds: int,
    spill_gib: int,
    url: str,
    target: str,
    parquet: Path,
) -> None:
    """Build date-local scalar geometry only; no live routing or publication."""
    from .chstore.client import Ch
    from .chstore.daily_scalar import build, manifest_bytes
    from .chstore.hot_l1_catalog import _unique_object

    if out.exists() or out.is_symlink() or not out.parent.is_dir():
        raise ValueError("daily scalar output must be fresh with an existing parent directory")
    if sort_spill_mib << 20 > (memory_gib << 30) // 4:
        raise ValueError("daily scalar sort threshold must not exceed one quarter of its memory cap")
    with source_descriptor.open("rb") as source:
        raw = source.read((64 << 10) + 1)
    if len(raw) > 64 << 10:
        raise ValueError("daily scalar descriptor exceeds 64 KiB")
    descriptor = json.loads(raw, object_pairs_hook=_unique_object)
    resume = None
    if resume_evidence is not None:
        with resume_evidence.open("rb") as source:
            resume_raw = source.read((64 << 10) + 1)
        if len(resume_raw) > 64 << 10:
            raise ValueError("daily scalar resume evidence exceeds 64 KiB")
        resume = json.loads(resume_raw, object_pairs_hook=_unique_object)
    ch = Ch(url)
    try:
        body = build(ch, target, parquet, descriptor, prefix=prefix, max_nodes=max_nodes,
                     memory_bytes=memory_gib << 30, spill_bytes=spill_gib << 30,
                     query_seconds=timeout_seconds, max_owned_bytes=owned_gib << 30,
                     min_free_bytes=reserve_gib << 30,
                     resume=resume, sort_spill_bytes=sort_spill_mib << 20, order_plan=order_plan,
                     progress=lambda message: print(message, file=sys.stderr, flush=True))
        with os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
            output.write(manifest_bytes(body))
        print(json.dumps({key: body[key] for key in ("schema", "complete", "date", "target", "prefix", "source_rows", "selected_source_rows", "nodes", "stages")} | {"out": str(out)}))
    finally:
        ch.close()


@main.command("ch-daily-scalar-check")
@option("-n", "--max-nodes", default=1000000, type=IntRange(min=1, max=1000000), help="Complete source oracle cap; never samples larger sources")
@option("-o", "--out", required=True, type=Path, help="Fresh private whole-subtree acceptance proof")
@option("-t", "--timeout-seconds", default=180, type=IntRange(min=1, max=600), help="Per-statement CH read deadline, not whole-file checksum time")
@option("-U", "--url", default="http://127.0.0.1:8123", help="Existing development ClickHouse HTTP endpoint")
@argument("manifest", type=Path)
@argument("parquet", type=Path)
def ch_daily_scalar_check(
    max_nodes: int,
    out: Path,
    timeout_seconds: int,
    url: str,
    manifest: Path,
    parquet: Path,
) -> None:
    """Verify every bounded scalar node against an independent Parquet oracle."""
    from .chstore.daily_scalar_check import check

    print(json.dumps(check(manifest, parquet, url, out, max_nodes=max_nodes, seconds=timeout_seconds)))


@main.command("ch-daily-scalar-audit-bench")
@option("-s", "--seconds", default=120, type=IntRange(min=1, max=300), help="Total read/compute wall budget; cleanup may use ten further seconds")
@option("-t", "--trials", default=1, type=IntRange(min=1, max=3), help="Original/fused pairs over the complete accepted source, alternating order")
@option("-U", "--url", default="http://127.0.0.1:8123", help="Existing development ClickHouse HTTP endpoint")
@argument("manifest", type=Path)
def ch_daily_scalar_audit_bench(
    seconds: int,
    trials: int,
    url: str,
    manifest: Path,
) -> None:
    """Compare bounded complete-tree audit emitters without writing source tables."""
    from .chstore.client import Ch
    from .chstore.daily_scalar_audit_bench import bench

    ch = Ch(url)
    try:
        print(json.dumps(bench(ch, manifest, trials=trials, seconds=seconds)))
    finally:
        ch.close()


@main.command("ch-name-summary-http-check")
@option("-c", "--clickhouse-url", required=True, help="ClickHouse HTTP endpoint for small startup identity reads only")
@option("-d", "--date", required=True, help="Pinned requested scan date")
@option("-D", "--from-date", help="Optional earlier pinned baseline scan")
@option("-f", "--logical-store", help="Explicit store binding; requires --dated-generation-root")
@option("-g", "--generation-root", required=True, type=Path, help="Private published generation root to pin once")
@option("-G", "--dated-generation-root", type=Path, help="Optional accepted daily root publication; requires --logical-store")
@option("-n", "--pattern", multiple=True, required=True, help="Selected literal; repeat, at most sixteen")
@option("-o", "--out", required=True, type=Path, help="New private compact whole-body acceptance artifact")
@option("-r", "--reference", multiple=True, type=Path, help="Explicit independently accepted cold L1 benchmark; repeat for every cold dated side")
@option("-t", "--trials", default=3, type=IntRange(min=1, max=10), help="Sequential responses per selected query")
@option("-T", "--token-env", default="QUERY_BOX_TOKEN", help="Bearer token environment variable; never logged")
@option("-w", "--timeout-seconds", default=8, type=FloatRange(min=0, min_open=True, max=8), help="Total HTTP response deadline, capped at eight seconds")
@option("-U", "--url", required=True, help="Explicit backend HTTP(S) origin, without path/query/credentials")
def ch_name_summary_http_check(
    clickhouse_url: str,
    date: str,
    from_date: str | None,
    logical_store: str | None,
    generation_root: Path,
    dated_generation_root: Path | None,
    pattern: tuple[str, ...],
    out: Path,
    reference: tuple[Path, ...],
    trials: int,
    token_env: str,
    timeout_seconds: float,
    url: str,
) -> None:
    """Check stitched HTTP bodies against pinned hot and independent cold refs."""
    from .chstore.name_summary_http_check import check

    if (dated_generation_root is None) != (logical_store is None):
        raise UsageError("--dated-generation-root and --logical-store are required together")
    print(json.dumps(check(generation_root, clickhouse_url, url, date, pattern, out,
                           references=reference, compare_from=from_date, trials=trials,
                           token_env=token_env, timeout=timeout_seconds,
                           dated_generation_root=dated_generation_root, logical_store=logical_store)))


@main.command("ch-hot-l2-http-bench")
@option("-a", "--all-registered", is_flag=True, help="Check every accepted predicate; mutually exclusive with -n")
@option("-d", "--date", required=True, help="Accepted scan date")
@option("-D", "--from-date", help="Optional accepted baseline; same/reversed comparisons allowed")
@option("-n", "--pattern", multiple=True, help="Registered literal; repeat or explicitly choose -a")
@option("-o", "--out", required=True, type=Path, help="Fresh private compact parity/timing artifact")
@option("-p", "--path", multiple=True, help="Declared bucket path; default all accepted buckets")
@option("-t", "--trials", default=1, type=IntRange(min=1, max=100), help="Sequential trials per predicate/bucket")
@option("-w", "--timeout-seconds", default=30, type=FloatRange(min=0, min_open=True, max=30), help="Total per-response HTTP deadline")
@option("-T", "--token-env", default="QUERY_BOX_TOKEN", help="Bearer token environment variable; never logged")
@option("-U", "--url", required=True, help="Explicit backend HTTP(S) origin, not a frontend default")
@argument("artifact", type=Path)
@argument("check", type=Path)
def ch_hot_l2_http_bench(
    all_registered: bool,
    date: str,
    from_date: str | None,
    pattern: tuple[str, ...],
    out: Path,
    path: tuple[str, ...],
    trials: int,
    timeout_seconds: float,
    token_env: str,
    url: str,
    artifact: Path,
    check: Path,
) -> None:
    """Whole-body HTTP parity against one accepted paired L2 ARTIFACT CHECK."""
    from .chstore.hot_l2_http_bench import bench

    print(json.dumps(bench(artifact, check, url, date, pattern, out, token_env=token_env,
                           paths=path, compare_from=from_date, trials=trials,
                           timeout=timeout_seconds, all_registered=all_registered)))


@main.command("ch-hot-l2-pair-compare")
@option("-o", "--out", required=True, type=Path, help="New private bound subset-parity proof; never overwrites")
@argument("reference", type=Path)
@argument("check", type=Path)
@argument("candidate", type=Path)
def ch_hot_l2_pair_compare(
    out: Path,
    reference: Path,
    check: Path,
    candidate: Path,
) -> None:
    """Compare accepted subset REFERENCE CHECK against a larger CANDIDATE."""
    from .chstore.hot_l2_pair_compare import compare

    body = compare(reference, check, candidate, out)
    print(json.dumps({**{key: body[key] for key in ('schema', 'complete', 'queries_compared', 'cells_compared', 'candidate_queries', 'candidate_cells', 'compare_s')}, 'out': str(out)}))


@main.command("ch-hot-l2-pair-check")
@option("-c", "--max-cells", default=4, type=IntRange(min=0, max=4), help="Optional scoped oracle cells, at most one per non-control query")
@option("-o", "--out", required=True, type=Path, help="New private acceptance artifact; never overwrites")
@option("-s", "--max-span", default=1_000_000, type=IntRange(min=1, max=1_000_000), help="Maximum union preorder span for a selected-cell oracle")
@option("-w", "--timeout-seconds", default=30, type=IntRange(min=15, max=30), help="Statement deadline, throwing rather than accepting partials")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("artifact", type=Path)
def ch_hot_l2_pair_check(
    max_cells: int,
    out: Path,
    max_span: int,
    timeout_seconds: int,
    url: str,
    artifact: Path,
) -> None:
    """Bounded paired L2 source checks; input provenance paths are operator-trusted."""
    from .chstore.hot_l2_pair_check import check

    body = check(url, artifact, out, seconds=timeout_seconds, max_span=max_span, max_cells=max_cells)
    print(json.dumps({'schema': body['schema'], 'dates': body['dates'], 'complete': body['complete'],
                      'covered_frames': body['covered_control']['frames_checked'],
                      'selected_cells_checked': sum(row['checked'] for row in body['selected_cells']), 'out': str(out)}))


@main.command("ch-hot-frequency-prune-bench")
@option("-a", "--census", required=True, type=Path, help="Accepted complete control census JSON")
@option("-d", "--date", required=True, help="Same frozen scan as the accepted export")
@option("-k", "--prune-chars", default=7, type=IntRange(min=1, max=31), help="Complete seed layer before pruning names once")
@option("-l", "--max-chars", type=IntRange(min=2, max=32), help="Last layer to validate; default only prune length plus one")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query memory budget")
@option("-o", "--out", required=True, type=Path, help="New private experiment JSON; never overwrites")
@option("-p", "--rss-pid", multiple=True, type=IntRange(min=1), help="Same-host RSS process to monitor")
@option("-q", "--queries", required=True, type=Path, help="Accepted complete hot-query JSONL export")
@option("-s", "--spill-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query temporary-disk budget")
@option("-t", "--threshold", required=True, type=IntRange(min=1), help="Exact same minimum threshold as the accepted export")
@option("-w", "--timeout-seconds", default=600, type=IntRange(min=1, max=600), help="Per-statement deadline; throws, never partial")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_hot_frequency_prune_bench(
    census: Path,
    date: str,
    prune_chars: int,
    max_chars: int | None,
    memory_gib: int,
    out: Path,
    rss_pid: tuple[int, ...],
    queries: Path,
    spill_gib: int,
    threshold: int,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Measure one seeded weighted-name prune, with complete per-layer parity."""
    from .chstore.hot_frequency_prune_bench import bench

    print(json.dumps(bench(url, target, date, threshold, prune_chars, census, queries, out,
                           max_chars=max_chars, memory_gib=memory_gib, seconds=timeout_seconds,
                           spill_gib=spill_gib, pids=rss_pid)))


@main.command("ch-pattern-frequency-census")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-m", "--memory-gib", default=8, type=IntRange(min=1, max=8), help="Offline per-query memory budget, not a serving constraint")
@option("-n", "--pattern", multiple=True, required=True, help="Selected name-only literal <=7 characters; at most16 queries")
@option("-o", "--rss-out", type=Path, help="New same-host RSS JSONL artifact; requires --rss-pid")
@option("-p", "--rss-pid", multiple=True, type=IntRange(min=1), help="Host process to monitor; repeat for simultaneous RSS")
@option("-q", "--profile", is_flag=True, help="Include completed query memory/I/O profile from system.query_log")
@option("-s", "--spill-gib", default=8, type=IntRange(min=1, max=16), help="Offline temporary-sort disk budget")
@option("-w", "--timeout-seconds", default=180, type=IntRange(min=1, max=600), help="Offline server query deadline; throws, never partial")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_pattern_frequency_census(
    date: str,
    memory_gib: int,
    pattern: tuple[str, ...],
    rss_out: Path | None,
    rss_pid: tuple[int, ...],
    profile: bool,
    spill_gib: int,
    timeout_seconds: int,
    url: str,
    target: str,
) -> None:
    """Read-only exact selected short-pattern frequencies, not coverage aggregates."""
    from .chstore.query_census import pattern_frequency_bench

    print(json.dumps(pattern_frequency_bench(url, target, date, pattern, memory_gib=memory_gib,
                                            seconds=timeout_seconds, spill_gib=spill_gib,
                                            pids=rss_pid, rss_out=rss_out, profile=profile)))


@main.command("ch-name-frequency-census")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-t", "--threshold", multiple=True, required=True, type=IntRange(min=1), help="Count exact names reused at least this many times")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_name_frequency_census(
    date: str,
    threshold: tuple[int, ...],
    url: str,
    target: str,
) -> None:
    """Read-only full-snapshot exact-name reuse, not substring support."""
    from .chstore.query_census import frequency_bench

    print(json.dumps(frequency_bench(url, target, date, threshold)))


@main.command("ch-short-query-census")
@option("-b", "--name-budget", default=100_000, type=IntRange(min=1, max=100_000), help="Maximum hash-sampled names; excess refuses before substring expansion")
@option("-n", "--max-chars", default=7, type=IntRange(min=1, max=7), help="Measure lengths 1 through this many Unicode characters")
@option("-s", "--sample-modulus", default=8192, type=IntRange(min=1), help="Keep names with cityHash64(nid) modulo this equal to zero")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_short_query_census(
    name_budget: int,
    max_chars: int,
    sample_modulus: int,
    url: str,
    target: str,
) -> None:
    """Short-query storage sizing; no sampled query-answer claims."""
    from .chstore.query_census import bench

    print(json.dumps(bench(url, target, max_chars, sample_modulus, name_budget)))


@main.command("ch-scoped-pattern-bench")
@option("-C", "--cold", is_flag=True, help="Reset data caches separately before construction, view and oracle")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-n", "--pattern", required=True, help="One case-insensitive basename substring literal")
@option("-p", "--path", required=True, help="Complete subtree; refuses a union interval over 1M nodes")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_scoped_pattern_bench(
    cold: bool,
    date: str,
    pattern: str,
    path: str,
    url: str,
    target: str,
) -> None:
    """Subtree-first broad leaf predicate screen; no live serving changes."""
    from .chstore.scoped_pattern import bench

    print(json.dumps(bench(url, target, date, path, pattern, cold=cold)))


@main.command("ch-identity-publish-bench")
@option("-g", "--groups", default=100, type=IntRange(min=1, max=10_000), help="Synthetic directories")
@option("-n", "--leaves", default=1000, type=IntRange(min=1), help="Initial files per directory; <=1M bootstrap paths")
@option("-o", "--out", required=True, type=Path, help="New private dev-node scratch directory; existing paths refused")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
def ch_identity_publish_bench(
    groups: int,
    leaves: int,
    out: Path,
    url: str,
) -> None:
    """Bounded synthetic durable-identity screen, not scan ingestion or FTS."""
    from .chstore.identity_publish import bench

    print(json.dumps(bench(url, out, groups, leaves)))


@main.command("ch-order-key-posting-history-bench")
@option("-b", "--before", required=True, help="Earlier frozen scan")
@option("-C", "--cold", is_flag=True, help="Reset caches independently before optimized/oracle reads")
@option("-d", "--date", required=True, help="Later frozen scan")
@option("-n", "--name", required=True, help="One exact leaf basename, case-insensitive")
@option("-p", "--path", required=True, help="Complete subtree scope; refuses more than 300K union postings")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_order_key_posting_history_bench(
    before: str,
    cold: bool,
    date: str,
    name: str,
    path: str,
    url: str,
    target: str,
) -> None:
    """Complete scoped historical scalar screen; no row sampling or TM claim."""
    from .chstore.key_history import posting_bench

    print(json.dumps(posting_bench(url, target, before, date, name, path, cold=cold)))


@main.command("ch-order-key-history-bench")
@option("-C", "--cold", is_flag=True, help="Reset data caches independently before delta/snapshot reads")
@option("-g", "--groups", default=100, type=IntRange(min=2, max=10_000), help="Synthetic directory count")
@option("-n", "--leaves", default=1000, type=IntRange(min=1), help="Initial leaves per directory; union capped at 300K")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
def ch_order_key_history_bench(
    cold: bool,
    groups: int,
    leaves: int,
    url: str,
) -> None:
    """Bounded stable-key additive-history screen, not incremental serving."""
    from .chstore.order_keys import history_bench

    print(json.dumps(history_bench(url, groups, leaves, cold=cold)))


@main.command("ch-order-key-sample-bench")
@option("-C", "--cold", is_flag=True, help="Reset data caches independently before each range read")
@option("-d", "--date", required=True, help="Frozen scan date")
@option("-n", "--sample-rows", default=100_000, type=IntRange(min=1, max=1_000_000), help="Explicit geometry sample cap; this is never full-query acceptance")
@option("-p", "--path", required=True, help="Existing subtree to sample in preorder")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_order_key_sample_bench(
    cold: bool,
    date: str,
    sample_rows: int,
    path: str,
    url: str,
    target: str,
) -> None:
    """Bounded real-data lineage geometry microbenchmark, not FTS parity."""
    from .chstore.order_keys import sample_bench

    print(json.dumps(sample_bench(url, target, date, path, sample_rows=sample_rows, cold=cold)))


@main.command("ch-order-key-bench")
@option("-C", "--cold", is_flag=True, help="Reset data caches independently before each numeric/lineage range read")
@option("-d", "--depth", default=12, type=IntRange(min=2, max=32), help="Root-to-leaf key length")
@option("-g", "--groups", default=100, type=IntRange(min=2, max=10_000), help="Synthetic directory count")
@option("-n", "--leaves", default=1000, type=IntRange(min=1, max=10_000), help="Files per directory; total must be <=1M leaves")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
def ch_order_key_bench(
    cold: bool,
    depth: int,
    groups: int,
    leaves: int,
    url: str,
) -> None:
    """Synthetic stable lineage-key geometry, not incremental serving."""
    from .chstore.order_keys import bench

    print(json.dumps(bench(url, groups, leaves, depth, cold=cold)))


@main.command("ch-stable-id-bench")
@option("-g", "--groups", default=100, type=IntRange(min=1, max=10_000), help="Synthetic new directory count")
@option("-n", "--leaves", default=1000, type=IntRange(min=1, max=10_000), help="Synthetic files per new directory; total input must be <=1M rows")
@option("-t", "--trials", default=3, type=IntRange(min=1, max=5), help="Repeat the same reservation/input and compare complete binding fingerprints")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
def ch_stable_id_bench(
    groups: int,
    leaves: int,
    trials: int,
    url: str,
) -> None:
    """Synthetic stable-ID staging only; no reservation or publication."""
    from .chstore.stable_ids import bench

    print(json.dumps(bench(url, groups, leaves, trials=trials)))


@main.command("ch-coverage-bench")
@option("-a", "--oracle-rows", default=100_000, type=IntRange(min=1, max=1_000_000), help="Independent QA candidate/child row budget; never affects contributing rows in the optimized answer")
@option("-b", "--before", "date0", help="Also verify a two-date coverage partition")
@option("-B", "--cold-build", is_flag=True, help="Reset data caches independently before each date's root-index construction")
@option("-c", "--compare", is_flag=True, help="Verify byte/object partitions with a full-path/string-ancestor oracle")
@option("-C", "--cold", is_flag=True, help="Reset data caches independently before views and oracles")
@option("-d", "--date", required=True, help="One frozen scan date")
@option("-g", "--root-plan", default="interval", type=Choice(["interval", "ancestry"]), help="Numeric interval dedup, or experimental selected-parent opaque-ID ancestry; query ranges still use frozen DFS")
@option("-k", "--child-budget", default=64, type=IntRange(min=1, max=256), help="Maximum heavy children")
@option("-n", "--pattern", required=True, help="One case-insensitive full-path substring literal without slashes")
@option("-o", "--out", help="Write private bodies to this remote scratch directory")
@option("-p", "--path", "paths", multiple=True, help="Explicit subtree; otherwise root and two large drills")
@option("-r", "--resident-roots", is_flag=True, help="Use packed outer-root prefix totals and rank selection without request-time root tables")
@option("-U", "--url", default="http://localhost:8123", help="Dev ClickHouse URL")
@argument("target")
def ch_coverage_bench(
    oracle_rows: int,
    date0: str | None,
    cold_build: bool,
    compare: bool,
    cold: bool,
    date: str,
    root_plan: str,
    child_budget: int,
    pattern: str,
    out: str | None,
    paths: tuple[str, ...],
    resident_roots: bool,
    url: str,
    target: str,
) -> None:
    """Directory-hit coverage prototype; bytes/objects only, no serving change."""
    from .chstore.coverage import bench

    print(json.dumps(bench(url, target, date, pattern, budget=child_budget, cold=cold, compare=compare, paths=paths, out=out, date0=date0, resident_roots=resident_roots, oracle_rows=oracle_rows, root_plan=root_plan, cold_build=cold_build)))


@main.command("ch-narrow-range-bench")
@option("-b", "--block-rows", default=4096, type=IntRange(min=1024), help="Preorder positions per sparse summary block")
@option("-C", "--cold", is_flag=True, help="Reset data caches independently before optimized/oracle reads; prefix remains resident")
@option("-d", "--date", required=True, help="One completed frozen historical scan")
@option("-n", "--name", required=True, help="One exact leaf basename, not substring search")
@option("-t", "--threads", default=8, type=IntRange(min=1), help="Equal query thread limits")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_range_bench(
    block_rows: int,
    cold: bool,
    date: str,
    name: str,
    threads: int,
    url: str,
    target: str,
) -> None:
    """Bounded scalar range-aggregation experiment; no live serving changes."""
    from .chstore.range_bench import bench

    print(json.dumps(bench(url, target, date, name, block_rows=block_rows, cold=cold, threads=threads)))


@main.command("ch-narrow-response-bench")
@option("-a", "--ancestor-preaggregate", is_flag=True, help="Experimental sibling aggregation before ancestor expansion; complete-body parity required")
@option("-B", "--bounded-joins", is_flag=True, help="Experimental 512-MiB GraceHash policy for response joins and disk-backed broad ancestor totals; root plan may override")
@option("-c", "--compare", is_flag=True, help="Verify complete parsed bodies against canonical historical serving")
@option("-C", "--cold", is_flag=True, help="Drop CH and OS caches before each experimental AND canonical response (dev node/root only)")
@option("-d", "--date", "dates", multiple=True, required=True, help="Selected scan date (repeatable)")
@option("-e", "--fold-parent-pruning", is_flag=True, help="Read folded ancestor links through visible parent IDs (A/B)")
@option("-f", "--compare-first", is_flag=True, help="Run canonical references once before timed repetitions, keeping warm optimized runs separate (requires -c)")
@option("-i", "--io-device", help="Same-host block device (e.g. sda): record whole-device counter deltas around each optimized response from /hostproc/diskstats")
@option("-j", "--directory-parent-index", "parent_index", is_flag=True, help="Use explicitly built immutable directory links for folded-parent lookup (A/B)")
@option("-J", "--root-join", type=Choice(["default", "hash", "grace_hash", "full_sorting_merge"]), default="default", help="Experimental rich-root join plan; explicit plans keep narrow roots on the right, grace_hash spills at 512 MiB")
@option("-k", "--visible-intervals", is_flag=True, help="Join rich roots to a bounded map of visible ancestor intervals (A/B)")
@option("-l", "--leaf-intervals", is_flag=True, help="Join interval endpoints only for non-leaf roots; leaf post equals pre (A/B)")
@option("-n", "--trials", default=2, type=int, help="Rounds per query/scan")
@option("-o", "--out", type=Path, help="Append compact per-response JSONL records")
@option("-p", "--previous", help="Produce a diff from this scan instead of a subtree")
@option("-P", "--no-path-free", "path_free", is_flag=True, flag_value=False, default=True, help="Disable the numeric-only candidate shortcut for single literal substrings (A/B baseline)")
@option("-q", "--query-id", "query_ids", multiple=True, help="Only these query IDs, in YAML order (repeatable); unknown IDs are refused")
@option("-Q", "--queries", required=True, help="Query YAML; uses the experiment's frozen prefix")
@option("-r", "--rich-name-index", "name_index", is_flag=True, help="Use the explicitly built rich name-order index for eligible literal roots (A/B)")
@option("-t", "--threads", default=8, type=int, help="ClickHouse threads")
@option("-T", "--comparison-dir", type=Path, default=Path("tmp/ch-response-references"), help="Scratch parent for temporary canonical body files with -f; files removed on exit")
@option("-u", "--ancestor-bottom-up", is_flag=True, help="Experimental scalar ancestor rollup one directory level at a time")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@option("-v", "--name-index-variant", help="Completed isolated rich name-index variant (requires -r); recorded in each response result")
@option("-w", "--reference-timeout", type=IntRange(min=1), help="Canonical benchmark-only per-statement wall-clock limit in seconds; throws on timeout, HTTP gets 60s grace; does not change live/optimized serving")
@argument("target")
def ch_narrow_response_bench(
    ancestor_preaggregate: bool,
    bounded_joins: bool,
    compare: bool,
    cold: bool,
    dates: tuple[str, ...],
    fold_parent_pruning: bool,
    compare_first: bool,
    io_device: str | None,
    parent_index: bool,
    root_join: str,
    visible_intervals: bool,
    leaf_intervals: bool,
    trials: int,
    out: Path | None,
    previous: str | None,
    path_free: bool,
    query_ids: tuple[str, ...],
    queries: str,
    name_index: bool,
    threads: int,
    comparison_dir: Path,
    ancestor_bottom_up: bool,
    url: str,
    name_index_variant: str | None,
    reference_timeout: int | None,
    target: str,
) -> None:
    """Complete historical subtree/diff bodies; initialization and serialization included."""
    from contextlib import nullcontext

    from .bench.queryset import load
    from .chstore import narrow, narrow_serve
    from .chstore.bench import drop_caches, normalize
    from .chstore.client import Ch
    from .chstore.response_bench import reference_bodies, save_mismatch
    from .chstore.resources import disk_delta, read_disk
    from .chstore.serve import Store

    narrow.identifier(target)
    if compare_first and not compare:
        raise UsageError("--compare-first requires --compare")
    if name_index_variant is not None:
        narrow.identifier(name_index_variant)
        if not name_index:
            raise UsageError("a rich name-index variant requires -r/--rich-name-index")
    manifest = json.loads(Ch(url, db=target).scalar("SELECT doc FROM history_manifest"))
    if any(d not in manifest["dates"] for d in (*dates, *([previous] if previous else []))):
        raise UsageError("all requested dates must be in the experimental history")
    store = Store(url, db=manifest["source_db"], threads=threads)
    cases = load(queries)
    if query_ids:
        unknown = sorted(set(query_ids) - {case.id for case in cases})
        if unknown:
            raise UsageError(f"unknown query IDs: {', '.join(unknown)}")
        cases = [case for case in cases if case.id in query_ids]
    if io_device is not None:
        read_disk(io_device)  # Refuse unavailable host counters before running references.
    budget = {"reference_timeout": reference_timeout} if reference_timeout is not None else {}
    root_plan = {"root_join": root_join} if root_join != "default" else {}
    if bounded_joins:
        root_plan["bounded_joins"] = True
    if leaf_intervals:
        root_plan["leaf_intervals"] = True
    if visible_intervals:
        root_plan["visible_intervals"] = True
    if fold_parent_pruning:
        root_plan["fold_parent_pruning"] = True
    if ancestor_bottom_up:
        root_plan["ancestor_bottom_up"] = True
    references = reference_bodies(store, dates, manifest["prefix"], cases, root=comparison_dir, previous=previous, **budget,
                                  reset=(lambda: drop_caches(url)) if cold else None, log=err) if compare_first else nullcontext({})
    with references as paths:
        for trial in range(trials):
            for date in dates:
                for case in cases:
                    if cold:
                        drop_caches(url)
                    io_before = read_disk(io_device) if io_device is not None else None
                    result = narrow_serve.response(url, target, date, case.q, previous=previous, syntax=case.qs, threads=threads,
                                                   path_free=path_free, name_index=name_index, parent_index=parent_index,
                                                   name_index_variant=name_index_variant, ancestor_preaggregate=ancestor_preaggregate, **root_plan)
                    if io_before is not None:
                        result["device_io"] = disk_delta(io_before, read_disk(io_device))
                    body = result.pop("body")
                    row = {"date": date, "previous": previous, "query": case.id, "query_text": case.q, "syntax": case.qs,
                           "trial": trial, "prefix": manifest["prefix"], "cold": cold, "threads": threads, **result,
                           "path_free": path_free, "name_index": name_index, "name_index_variant": name_index_variant,
                           "parent_index": parent_index, "ancestor_preaggregate": ancestor_preaggregate,
                           "comparison_phase": "first" if compare_first else "interleaved" if compare else "none",
                           "sha": normalize(json.dumps(body).encode())}
                    if reference_timeout is not None:
                        row["reference_timeout_s"] = reference_timeout
                    if root_join != "default":
                        row["root_join"] = root_join
                    if bounded_joins:
                        row["bounded_joins"] = True
                    if leaf_intervals:
                        row["leaf_intervals"] = True
                    if visible_intervals:
                        row["visible_intervals"] = True
                    if fold_parent_pruning:
                        row["fold_parent_pruning"] = True
                    if ancestor_bottom_up:
                        row["ancestor_bottom_up"] = True
                    if compare:
                        if compare_first:
                            with paths[date, case.id].open() as reference:
                                baseline = json.load(reference)
                        else:
                            err(f"optimized {date} / {case.id}: {row['response_s']}s (canonical verification pending)")
                            if cold:
                                drop_caches(url)
                            baseline = narrow_serve.compare_response(store, date, manifest["prefix"], case.q, previous=previous, syntax=case.qs, **budget)
                        canonical_body = baseline.pop("body")
                        row["exact"] = body == canonical_body
                        if not row["exact"]:
                            row["mismatch_dir"] = str(save_mismatch(comparison_dir, body, canonical_body))
                        row["baseline"] = baseline
                    line = json.dumps(row)
                    print(line, flush=True)
                    if out:
                        with out.open("a") as f:
                            f.write(line + "\n")
                    if compare and not row["exact"]:
                        raise ValueError(f"complete response mismatch: {date} / {previous} / {case.id}")


@main.command("ch-narrow-summary")
@option("-n", "--nonempty", is_flag=True, help="Only queries with at least one root")
@argument("path")
def ch_narrow_summary(nonempty: bool, path: str) -> None:
    """Summarize a frozen ID benchmark JSONL (PATH or - for stdin), without root lists."""
    from contextlib import nullcontext

    import fsspec

    from .chstore.narrow import summarize

    with nullcontext(sys.stdin) if path == "-" else fsspec.open(path, "r") as f:
        rows = (json.loads(line) for line in f if line.strip())
        print(json.dumps(summarize(row for row in rows if not nonempty or row["n"] > 0), indent=2))


@main.command("ch-narrow-compare")
@argument("before", type=Path)
@argument("after", type=Path)
def ch_narrow_compare(before: Path, after: Path) -> None:
    """Pair saved uncapped discovery/component JSONL; NOT complete responses."""
    from .chstore.narrow import compare_discovery_runs

    records = []
    for path in (before, after):
        with path.open() as f:
            records.append([json.loads(line) for line in f if line.strip()])
    print(json.dumps(compare_discovery_runs(*records), indent=2))


@main.command("ch-narrow-parent-check")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_parent_check(url: str, target: str) -> None:
    """Read-only sorted parent validation; heavy, do not overlap remote jobs."""
    from time import monotonic

    from .chstore.client import Ch
    from .chstore.narrow import missing_parent_keys

    ch = Ch(url, timeout=7200)
    try:
        start = monotonic()
        missing = missing_parent_keys(ch, target)
        if missing:
            raise ValueError(f"union lacks parents: {missing}")
        print(json.dumps({"target": target, "missing": missing, "seconds": round(monotonic() - start, 3)}))
    finally:
        ch.close()


@main.command("ch-narrow-progress")
@option("-U", "--url", default="http://localhost:8123", help="ClickHouse URL (an SSH forward is sufficient)")
@argument("target")
def ch_narrow_progress(url: str, target: str) -> None:
    """Read-only active stages, table sizes, disk reserve and recent failures."""
    from .chstore.narrow import progress

    print(json.dumps(progress(url, target), indent=2))


@main.command("ch-narrow-audit")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_audit(url: str, target: str) -> None:
    """Read-only full-domain numbering/count audit. Heavy: run on a dev node,
    separately from construction and latency benchmarks; not query truth."""
    from .chstore.narrow import audit

    print(json.dumps(audit(url, target)))


@main.command("ch-narrow-response-summary")
@argument("path")
def ch_narrow_response_summary(path: str) -> None:
    """Summarize complete-response experiment JSONL (PATH or - for stdin)."""
    from contextlib import nullcontext

    import fsspec

    from .chstore.narrow_serve import summarize

    with nullcontext(sys.stdin) if path == "-" else fsspec.open(path, "r") as f:
        print(json.dumps(summarize([json.loads(line) for line in f if line.strip()]), indent=2))


@main.command("ch-narrow-query-profile")
@option("-m", "--min-ms", default=100, type=IntRange(min=0), help="Minimum completed-statement duration in milliseconds")
@option("-n", "--limit", default=50, type=IntRange(min=1), help="Maximum recent statements")
@option("-s", "--seconds", default=900, type=IntRange(min=1), help="Query-log lookback in seconds; no log flush or query replay")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("target")
def ch_narrow_query_profile(
    min_ms: int,
    limit: int,
    seconds: int,
    url: str,
    target: str,
) -> None:
    """Read recent numeric/history-view statement costs; SQL may include private paths."""
    from .chstore.narrow import query_profile

    print(json.dumps(query_profile(url, target, seconds=seconds, limit=limit, min_ms=min_ms), indent=2))


@main.command("ch-narrow-response-compare")
@argument("before", type=Path)
@argument("after", type=Path)
def ch_narrow_response_compare(before: Path, after: Path) -> None:
    """Pair complete-body A/B records, requiring equal workloads and bodies."""
    from .chstore.narrow_serve import compare_runs

    records = []
    for path in (before, after):
        with path.open() as f:
            records.append([json.loads(line) for line in f if line.strip()])
    print(json.dumps(compare_runs(*records), indent=2))


@main.command("ch-ingest")
@option("-a", "--allow-drop", is_flag=True, help="Ingest a scan lacking roots (buckets) the store has, closing them (else refused as partial)")
@option("-B", "--db", default=None, help="The store's database (default: $CLICKHOUSE_DB, else `default`)")
@option("-d", "--date", "scan_id", required=True, help="The scan id (`YYYY-MM-DD[THHMM]`)")
@option("-f", "--force", is_flag=True, help="Ingest a scan of fewer than half the open rows (else refused as partial)")
@option("-F", "--server-file", is_flag=True, help="SRC is a path under the ClickHouse server's `user_files_path`, read server-side (fastest on the box itself)")
@option("-g", "--data-bucket", default=None, help="Where the default SRC lives (default: $DATA_BUCKET)")
@option("-j", "--pairs", default=None, type=IntRange(min=1), help="Simultaneous ingest key ranges; overrides CH_INGEST_PAIRS (else half the threads)")
@option("-s", "--stage", type=Path, default=None, help="Copy a gs:// SRC here first (it's then streamed to the server)")
@option("-t", "--threads", default=8, type=int, help="ClickHouse `max_threads` / `max_insert_threads`")
@option("-U", "--url", default=None, help="ClickHouse's HTTP endpoint (default: $CLICKHOUSE_URL, else http://localhost:8123)")
@argument("src", required=False)
def ch_ingest(allow_drop: bool, db: str | None, scan_id: str, force: bool, server_file: bool, data_bucket: str | None, pairs: int | None, stage: Path | None, threads: int,
              url: str | None, src: str | None) -> None:
    """Ingest one scan into the ClickHouse store (specs/ch-store.md §3): its
    path store's `path` sort (SRC: a v2 store generation or a v1 index;
    default the newest generation's under `gs://<bucket>/listing/<date>/index/`)
    diffed against the open versions — new versions opened, changed and
    deleted ones closed. Idempotent: an ingested scan is a no-op, an
    interrupted one is redone. Prints the scan's record (rows, versions
    opened / closed, seconds per step) as JSON."""
    import os as _os

    from .chstore import ingest as ci
    from .chstore.client import DEFAULT_URL, Ch

    if src is None:
        bucket = data_bucket or _os.environ.get("DATA_BUCKET")
        if not bucket:
            raise UsageError("no SRC and no data bucket (-g / $DATA_BUCKET)")
        src = ci.default_src(bucket, scan_id)
        err(f"ch-ingest {scan_id}: {src}")
    ch = Ch(url or _os.environ.get("CLICKHOUSE_URL") or DEFAULT_URL, db=db or _os.environ.get("CLICKHOUSE_DB") or "default", timeout=7200)
    try:
        rec = ci.Ingest(ch, scan_id, src, server_file=server_file, force=force, allow_drop=allow_drop, threads=threads, pairs=pairs, stage_dir=stage).run()
    except ci.IngestError as e:
        raise UsageError(str(e)) from e
    rec["sizes"] = ci.sizes(ch)
    print(json.dumps(rec))


@main.command("ch-ingest-plan-bench")
@option("-B", "--db", default="default", help="Published historical store database")
@option("-d", "--date", "scan_id", required=True, help="Published scan to replay as read-only changed-row SELECTs")
@option("-i", "--range-index", "indices", multiple=True, required=True, type=IntRange(min=0), help="Zero-based sampled key range; repeat for several bounded cases")
@option("-n", "--trials", default=2, type=IntRange(min=1), help="Rounds, alternating plan order")
@option("-o", "--out", type=Path, help="New JSONL output file; refuses existing files")
@option("-s", "--samples", default=128, type=IntRange(min=1), help="Primary-mark samples used to derive bounded key ranges")
@option("-t", "--threads", default=4, type=IntRange(min=1), help="Equal thread limit for all compared plans")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("server_file")
def ch_ingest_plan_bench(
    db: str,
    scan_id: str,
    indices: tuple[int, ...],
    trials: int,
    out: Path | None,
    samples: int,
    threads: int,
    url: str,
    server_file: str,
) -> None:
    """Read-only epoch/spill/join A/B over an independently audited staged source.

    Does not ingest, upload, reset caches or publish changes. Verification
    compares full sorted-row fingerprints separately from timed SELECTs.
    """
    from contextlib import nullcontext

    from .chstore.client import Ch
    from .chstore.ingest_bench import benchmark

    ch = Ch(url, db=db, timeout=7200, max_threads=threads, max_memory_usage=8 << 30)
    try:
        with out.open("x") if out else nullcontext() as output:
            def emit(row: dict) -> None:
                line = json.dumps(row)
                print(line, flush=True)
                if output is not None:
                    output.write(line + "\n")
                    output.flush()

            benchmark(ch, scan_id, server_file, indices, samples=samples, threads=threads, trials=trials, emit=emit)
    finally:
        ch.close()


@main.command("ch-ingest-root-audit")
@option("-B", "--db", default="default", help="Published historical store database")
@option("-d", "--date", "scan_id", required=True, help="Exact published scan id")
@option("-t", "--threads", default=2, type=IntRange(min=1), help="ClickHouse query threads")
@option("-U", "--url", default="http://localhost:8123", help="Dev node ClickHouse URL")
@argument("server_file")
def ch_ingest_root_audit(
    db: str,
    scan_id: str,
    threads: int,
    url: str,
    server_file: str,
) -> None:
    """Compare every bucket/owner root value with an already-staged source.

    Read-only; does not validate all descendants or publish a scan. SERVER_FILE
    is relative to the ClickHouse server's user_files_path.
    """
    from .chstore.client import Ch
    from .chstore.ingest import IngestError, audit_roots

    ch = Ch(url, db=db, max_threads=threads, max_memory_usage=1 << 30)
    try:
        print(json.dumps(audit_roots(ch, scan_id, server_file)))
    except IngestError as error:
        raise UsageError(str(error)) from error
    finally:
        ch.close()


def bucket_sources(specs: tuple[str, ...], default_bucket: str) -> list[tuple[str, str]]:
    """`<bucket>=<layer-2 parquet>` pairs → [(bucket, path)]; a bare path is
    ``default_bucket``'s (the single-bucket form)."""
    out: list[tuple[str, str]] = []
    for s in specs:
        bucket, eq, path = s.partition("=")
        out.append((bucket, path) if eq else (default_bucket, s))
    return out


@main.command("index-write")
@option("-A", "--age-only", is_flag=True, help="Only the age pyramid — skip the store's sorts (a ladder-only backfill; sync with `index-sync -A`, the sort pointers keep their generation)")
@option("-b", "--bucket", default=None, help="Bucket a bare (no `<bucket>=`) layer-2 argument describes (default $CW_BUCKET)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Output dir: path-index.parquet + path-index-bysize.parquet (+ .groups.json sidecars) + age-pyramid-*.parquet")
@option("-r", "--row-group-rows", default=8192, type=int, help="Parquet row-group size for the sorts — the range-read unit and the D1 footer's row count per sort (default 8192; a gcs-sized fleet uses 32768, specs/path-store.md §1.6)")
@option("-S", "--search", is_flag=True, help="Also write the search sidecars beside the `path` sort (`path-index.{rows,trigrams,rows-search}.parquet`, specs/path-store-search.md)")
@option("-t", "--threads", default=8, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: <out>/.duckdb-tmp)")
@argument("sources", nargs=-1, required=True)
def index_write(age_only: bool, bucket: str | None, mem: str, out_dir: Path, row_group_rows: int, search: bool, threads: int, tmp_dir: Path | None, sources: tuple[str, ...]) -> None:
    """Write the scan's path store from its layer-2 parquet(s) — SOURCES are
    `<bucket>=<l2.parquet>` pairs, one per bucket of the scan (a bare path is
    `-b`'s bucket): every row (objects and dirs), bucket-prefixed, in the
    layer-2's column names, cut by the engine into the `path` sort
    (`path-index.parquet`, `(depth, path)`) and the `bysize` sort
    (`path-index-bysize.parquet`, `(⌊log2 size⌋ desc, path)`), 8k-row groups,
    footer sidecars beside them, plus the age pyramid (specs/path-store.md
    §4.2). `index-sync` then publishes their footers to D1."""
    from .index import write_index
    from .sweep import CW_BUCKET

    s = write_index(
        bucket_sources(sources, bucket or CW_BUCKET), out_dir,
        mem=mem, threads=threads, tmp_dir=tmp_dir, age_only=age_only, row_group_rows=row_group_rows, search=search,
    )
    if age_only:
        err(f"index-write: age pyramid only — floor {s['pyramid']['floor']}, {len(s['pyramid']['bins'])} tiers over {s['buckets']}")
    else:
        err(f"index-write: {s['rows']:,} rows over {s['buckets']}; sorts {s['sorts']}")
    print(json.dumps(s))


@main.command("over-time-groups")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket the D1 `path` dirs resolve against")
@option("-g", "--gen", required=True, help="Generation id for the published index dirs (the run's, e.g. the job's $GEN)")
@option("-K", "--group-size", default=None, type=int, help="Scans per sealed group (default: dt_cloud.overtime.OVER_TIME_GROUP_SIZE)")
@option("-l", "--layer2-prefix", default=None, help="Layer-2 dir template with `{scan}` (default: $LAYER2_PREFIX, e.g. cw-l2/{scan}/)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-n", "--dry-run", is_flag=True, help="Print the groups that would be built; write nothing")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Work dir for the group builds")
@option("-p", "--publish-root", default=None, help="Where the published dirs live (default /gcs/<bucket>, the Batch mount); groups land at <root>/<layer2>/index/<gen>/")
@option("-r", "--data-root", default=None, help="Root the D1 `path` pointer dirs resolve against (default: the publish root)")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-t", "--threads", default=8, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: <out>/.duckdb-tmp)")
def over_time_groups(
    bucket: str,
    gen: str,
    group_size: int | None,
    layer2_prefix: str | None,
    mem: str,
    dry_run: bool,
    out_dir: Path,
    publish_root: str | None,
    data_root: str | None,
    store: str,
    threads: int,
    tmp_dir: Path | None,
) -> None:
    """Seal the next over-time groups (specs/obs-axis-indexing.md Phase 1, the
    capped-K shape): partition every scan with a synced `path` index into fixed
    K-scan groups, oldest first, and for each group not yet in the manifest
    build its over-time MS, publish it under the group's LAST scan's layer-2 dir
    (`<layer2>/index/<gen>/over-time.parquet` + scans sidecar), sync the footer
    (`index_schema`/`index_row_groups` as variant `over-time`) and, last, write
    the `pyramid_multiscans` row the site routes by. Idempotent: sealed groups
    never change, so a re-run only appends. Prints `{"groups": [<gid>, …]}` for
    the caller to `publish-r2` each new group's scan dir. The < K tail is served
    by `/api/series`'s per-scan fallback."""
    import shutil
    import time

    from .index_footer import index_dir, sync_d1, synced_variants
    from .overtime import OVER_TIME_GROUP_SIZE, multiscan_row, sealed_groups, sync_manifest, synced_groups, write_over_time_index

    K = group_size or OVER_TIME_GROUP_SIZE
    l2 = layer2_prefix or os.environ.get("LAYER2_PREFIX") or "cw-l2/{scan}/"
    root = publish_root or f"/gcs/{bucket}"
    data = data_root or root
    dates = sorted({d for d, v in synced_variants(store=store) if v == "path"})
    done = synced_groups(store=store)
    todo = [g for g in sealed_groups(dates, K) if g[-1] not in done]
    err(f"over-time-groups: {len(dates)} indexed scans → {len(sealed_groups(dates, K))} sealed groups of {K}, {len(done)} in the manifest, {len(todo)} to build")
    if dry_run:
        for g in todo:
            err(f"  would build {g[-1]}: {g[0]}..{g[-1]} ({len(g)} scans)")
        print(json.dumps({"groups": [g[-1] for g in todo], "dry_run": True}))
        return
    built: list[str] = []
    for g in todo:
        gid = g[-1]
        pairs: list[tuple[str, str]] = []
        for d in g:
            dir_ = index_dir(d, "path", store=store)
            if dir_ is None:
                raise SystemExit(f"over-time-groups: no `path` pointer for {d} (group {gid})")
            pairs.append((d, f"{data}/{dir_}/path-index.parquet"))
        summ = write_over_time_index(pairs, out_dir / gid, mem=mem, threads=threads, tmp_dir=tmp_dir)
        key = f"{l2.format(scan=gid).rstrip('/')}/index/{gen}"
        dest = Path(root) / key
        dest.mkdir(parents=True, exist_ok=True)
        for f in (summ["file"], summ["scans_file"]):
            shutil.copy2(f, dest / Path(f).name)
        n = sync_d1(gid, str(dest / Path(summ["file"]).name), variant="over-time", gen=gen, key=key, store=store)
        sync_manifest(multiscan_row(g, written_at_ms=int(time.time() * 1000), store=store))
        err(f"over-time-groups: sealed {gid} ({g[0]}..{gid}, {len(g)} scans, {summ['rows']:,} intervals, {n} row groups) → {key}")
        built.append(gid)
    print(json.dumps({"groups": built}))


@main.command("churn")
@option("-c", "--column", "columns", multiple=True, help="Only compare these value columns (repeatable; default: every non-key column both files share)")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Also write the delta (`delta.parquet`, `delta-objects.parquet`) here and report its bytes")
@option("-t", "--threads", default=4, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: DuckDB's)")
@argument("a")
@argument("b")
def churn_cmd(columns: tuple[str, ...], mem: str, out_dir: Path | None, threads: int, tmp_dir: Path | None, a: str, b: str) -> None:
    """Rows added / removed / changed from scan A to scan B (specs/storage-consolidation.md
    phase 3): A and B are two `path` sorts (or layer-2s) — local paths; stage
    remote ones first. Keyed `(depth, path)` (+ an owner label both carry),
    joined one depth at a time; counts per `kind` and per changed column, as
    JSON on stdout."""
    from .churn import scan_churn

    con = duckdb.connect()
    con.execute(f"SET memory_limit='{mem}'; SET threads={threads}")
    if tmp_dir is not None:
        con.execute(f"SET temp_directory='{tmp_dir}'")
    print(json.dumps(scan_churn(a, b, out_dir=out_dir, columns=list(columns) or None, con=con), indent=1))


@main.command("over-time-churn")
@argument("groups", nargs=-1, required=True)
def over_time_churn_cmd(groups: tuple[str, ...]) -> None:
    """Per-scan dir churn (changed / added / removed paths at each scan boundary)
    of sealed over-time groups (`over-time.parquet`, local paths), as JSON lines
    on stdout — one object per group."""
    from .churn import group_churn

    con = _connect()
    for g in groups:
        print(json.dumps({"group": g, **group_churn(g, con=con)}))


@main.command("index-sync")
@option("-A", "--age-only", is_flag=True, help="Only the age-pyramid variants (a ladder-only backfill; the other variants keep their pointer)")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket holding the index tiers")
@option("-d", "--dir", "listing_dir", default=None, help="Local/mounted dir holding the parquets (default: <bucket>/<key>)")
@option("-F", "--sorts-only", is_flag=True, help="Only the store's sorts (path, bysize, and their by-user copies where written)")
@option("-g", "--gen", required=True, help="Generation stamp these files belong to (the run's GEN; `legacy` for the pre-generation listing/<date>/ layout)")
@option("-k", "--key", default=None, help="Bucket-relative dir the parquets live under — what the site reads (default: listing/<date>/index/<gen>; listing/<date> for gen `legacy`)")
@option("-L", "--local", is_flag=True, help="Write to the local wrangler D1 instead of --remote")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-v", "--variant", "variants", multiple=True, type=Choice(list(INDEX_VARIANTS)), help="Only sync these variants (default: all)")
@argument("date")
def index_sync(
    age_only: bool,
    bucket: str,
    listing_dir: str | None,
    sorts_only: bool,
    gen: str,
    key: str | None,
    local: bool,
    store: str,
    variants: tuple[str, ...],
    date: str,
) -> None:
    """Publish a scan's index-tier footers to D1 (index_row_groups + the
    index_schema pointer) — one generation of files under one bucket dir.
    Per variant the row groups land first, tagged with the generation, and the
    pointer (gen, dir) flips last, so the site moves from the previous complete
    generation to this one with no window (specs/view-serving.md). Needs
    CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in the env. `--store` files
    the rows under a secondary store (variants recorded as
    `<store>:<variant>`, the `store` column set — needs the store migration;
    specs/multi-store.md). The default, the primary, writes exactly the
    pre-stores SQL."""
    from .index_footer import SORT_VARIANTS, check_store, exists, sync_d1

    try:
        check_store(store)
    except ValueError as e:
        raise UsageError(str(e)) from None

    key = key or (f"listing/{date}" if gen == "legacy" else f"listing/{date}/index/{gen}")
    base = listing_dir or f"{bucket}/{key}"
    todo = variants or tuple(INDEX_VARIANTS)
    if sorts_only:
        todo = tuple(v for v in todo if v in SORT_VARIANTS)
    if age_only:
        todo = tuple(v for v in todo if v.startswith("age-pyramid"))
    # A deployment produces only some variants (gcs's `path-index` writes no
    # age pyramid or over-time tier; a generation before the store has no
    # `bysize`); an absent file is skipped, not fatal — otherwise every variant
    # after it in `INDEX_VARIANTS` order went unsynced.
    skipped = []
    for variant in todo:
        path = f"{base}/{INDEX_VARIANTS[variant]}"
        if not exists(path):
            skipped.append(variant)
            continue
        n = sync_d1(date, path, variant=variant, gen=gen, key=key, remote=not local, store=store)
        err(f"index-sync: {'' if store == 'primary' else f'[{store}] '}{date} [{variant}] gen {gen} @ {key} — {n} row groups ({'local' if local else 'remote'})")
    if skipped:
        err(f"index-sync: {date} gen {gen} @ {key} — skipped {len(skipped)} absent variant(s): {', '.join(skipped)}")
    if len(skipped) == len(todo):
        err(f"index-sync: no variant file under {base}")
        raise SystemExit(1)


@main.command("index-gc")
@option("-b", "--base", default="oa-gcs-usage-dvx", help="Where the pointers' dirs live, for -r's cold-footer check: the data bucket (default oa-gcs-usage-dvx), a mounted dir, or an fsspec URL (`r2://bucket`)")
@option("-F", "--files", "targets", multiple=True, help="Also delete the files of generation dirs no pointer names (repeatable): `gs://<bucket>` (the scan store), `r2` (the R2 serving bucket: `$R2_BUCKET`, via publish-r2's `R2_*` env) or `r2://<bucket>`. Only under scans with a `path` pointer; never a pointed dir; never one younger than -m")
@option("-m", "--min-age", default="2d", help="-F's grace period: a generation whose newest object is younger is kept, so an in-flight reindex is never raced; e.g. 36h, 2d (default)")
@option("-n", "--dry-run", is_flag=True, help="Print what would go (D1 rows counted; generation dirs per store with bytes, and totals); delete nothing")
@option("-r", "--retain", type=int, default=None, help="Retention: also retire the store sorts' row groups of every scan older than the newest N whose `.groups.parquet` exists (their pointers stay; the reader range-reads that cold footer instead). A variant without one keeps its rows (warned): backfill it with `index-blob`")
@option("-R", "--no-rows", is_flag=True, help="Skip the D1 row sweep (e.g. an -F pass over every scan after the job's per-scan row sweep)")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-w", "--workers", default=8, type=int, help="-F: concurrent listings (default 8)")
@argument("dates", nargs=-1)
def index_gc(
    base: str,
    targets: tuple[str, ...],
    min_age: str,
    dry_run: bool,
    retain: int | None,
    no_rows: bool,
    store: str,
    workers: int,
    dates: tuple[str, ...],
) -> None:
    """Delete row groups of index generations no pointer names — a REPROC's
    previous generation, or a sync that died before flipping. All synced
    scans by default; DATES to restrict. With -r, the retention pass too.

    With -F, also those generations' files (`<layer-2>/index/<gen>/`) in each
    target store (`dt_cloud.gen_gc`): dirs no `index_schema` row names, under
    scans that have a `path` pointer, older than -m. E.g. the backlog over
    every scan, GCS + R2, dry run first:

        dt-cloud index-gc -R -n -F gs://oa-gcs-usage-dvx -F r2
    """
    import time

    from .gen_gc import open_store, parse_age, sweep
    from .index_footer import d1_variant, gc_d1, pointers, retire_d1, synced_variants

    if dry_run and retain is not None:
        raise UsageError("-n covers the row sweep and -F, not -r")
    try:
        grace = parse_age(min_age)
    except ValueError as e:
        raise UsageError(str(e)) from None
    stores = [open_store(t) for t in targets]
    if not no_rows:
        todo = dates or sorted({d for d, _ in synced_variants(store=store)})
        for d in todo:
            n = gc_d1(d, store=store, dry_run=dry_run)
            err(f"index-gc: {d} — {n} stale row groups {'would be ' if dry_run else ''}deleted")
    if stores:
        sweep(
            pointers(), stores,
            now=time.time(), min_age=grace, dry_run=dry_run, reread=pointers,
            path_variant=d1_variant("path", store), dates=dates or None, workers=workers,
        )
    if retain is not None:
        retired, skipped = retire_d1(retain, store=store, base=base)
        for d, v, n in retired:
            err(f"index-gc: retired {d} [{v}] — {n} row groups (its .groups.parquet serves it now)")
        for d, v, missing in skipped:
            err(f"index-gc: WARNING kept {d} [{v}] in D1 — no cold footer at {missing} (backfill: dt-cloud index-blob -P {d})")


@main.command("index-dir")
@option("-s", "--store", default="primary", help="The store these index rows belong to (specs/multi-store.md): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-v", "--variant", default="path", type=Choice(list(INDEX_VARIANTS)), help="Which variant's dir")
@argument("date")
def index_dir_cmd(store: str, variant: str, date: str) -> None:
    """Print the bucket-relative dir holding a scan's index variant (the D1
    pointer). Exits 1, printing nothing, when that (date, variant) was never
    synced."""
    from .index_footer import index_dir

    d = index_dir(date, variant, store=store)
    if d is None:
        raise SystemExit(1)
    print(d)


@main.command("labels")
@option("-a", "--attribution", "attributions", multiple=True, help="Attribution parquet(s) (as `path-index -a`)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, default=None, help=f"identities.yaml path or URL, needed with -a (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-l", "--listing", "listings", required=True, multiple=True, help="Listing parquet glob(s) — path-glob rules expand against their dirs")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Output dir: one labels-<bucket>.parquet per bucket")
def labels(attributions: tuple[str, ...], identities_path: str | None, listings: tuple[str, ...], out_dir: Path) -> None:
    """Export mgu's attribution as DT label tables — `(prefix, usr)` per bucket,
    prefix relative to the bucket — for `disk-tree import -e duckdb -L
    labels-<bucket>.parquet -c usr` (spec specs/done/mgu-scale-unification.md §B): the
    same prefix map `path-index` attributes with, so the two cascades can be
    compared slice for slice."""
    import duckdb

    from .viz import write_labels

    con = duckdb.connect()
    for bucket, n in write_labels(con, listings, attributions, identities_path, out_dir).items():
        err(f"labels: {bucket}: {n} prefixes → {out_dir / f'labels-{bucket}.parquet'}")


@main.command("index-blob")
@option("-a", "--all", "all_dates", is_flag=True, help="Every scan with a synced pointer (instead of DATES)")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket holding the index tiers")
@option("-d", "--dir", "listing_dir", default=None, help="Local/mounted/gs:// dir holding the parquets (default: gs://<bucket>/<key>); one DATE only")
@option("-g", "--gen", default=None, help="Generation the files belong to (`legacy` for listing/<date>/); default: each variant's D1 pointer dir")
@option("-J", "--from-json", is_flag=True, help="Build the .groups.parquet from the .groups.json already beside the tier, not the tier's own footer (implies -P)")
@option("-k", "--key", default=None, help="Bucket-relative dir the parquets live under (default: listing/<date>/index/<gen>; listing/<date> for gen `legacy`)")
@option("-n", "--dry-run", is_flag=True, help="Print what would be written; write nothing")
@option("-P", "--parquet-only", is_flag=True, help="Only the cold footer tier (`.groups.parquet`); leave the .groups.json as it is")
@option("-s", "--store", default="primary", help="The store whose pointers name the dirs (no -g): `primary` (default) or a secondary store's `STORES_JSON` key")
@option("-S", "--sorts-only", is_flag=True, help="Only the store's sorts (`SORT_VARIANTS`: what `index-gc -r` retires)")
@option("-v", "--variant", "variants", multiple=True, type=Choice(list(INDEX_VARIANTS)), help="Only these variants (default: all)")
@argument("dates", nargs=-1)
def index_blob(
    all_dates: bool,
    bucket: str,
    listing_dir: str | None,
    gen: str | None,
    from_json: bool,
    key: str | None,
    dry_run: bool,
    parquet_only: bool,
    store: str,
    sorts_only: bool,
    variants: tuple[str, ...],
    dates: tuple[str, ...],
) -> None:
    """Write each tier's footer sidecars beside its parquet — the cold footer
    tier `<tier>.groups.parquet` and the `<tier>.groups.json` blob, the rows
    `index-sync` puts in D1. The backfill for generations synced before
    `index-sync` wrote them: `index-gc -r` retires a scan's rows from D1 only
    once its `.groups.parquet` exists, and the site then range-reads it
    (specs/path-store.md §1.6). E.g. before lowering retention on gcs:

        dt-cloud index-blob -a -S -P

    Each variant's dir comes from `-d`/`-k`/`-g`, else its D1 pointer; an
    absent tier (a deployment writes only some variants) is skipped."""
    from .index_footer import SORT_VARIANTS, exists, extract, groups_parquet_path, index_dir, read_groups_blob, synced_variants, write_groups_blob, write_groups_parquet

    if all_dates == bool(dates):
        raise UsageError("give DATES or -a, not both")
    if listing_dir and (all_dates or len(dates) > 1):
        raise UsageError("-d names one scan's dir: one DATE only")
    by_pointer = not (listing_dir or gen is not None or key is not None)
    synced = synced_variants(store=store) if all_dates or by_pointer else []
    todo_dates = sorted({d for d, _ in synced}) if all_dates else list(dates)
    todo_vars = variants or tuple(INDEX_VARIANTS)
    if sorts_only:
        todo_vars = tuple(v for v in todo_vars if v in SORT_VARIANTS)
    have = set(synced)
    for date in todo_dates:
        for variant in todo_vars:
            if listing_dir:
                base = listing_dir
            elif gen is not None or key is not None:
                k = key or (f"listing/{date}" if gen == "legacy" else f"listing/{date}/index/{gen}")
                base = f"gs://{bucket}/{k}"
            else:
                if (date, variant) not in have:
                    continue
                k = index_dir(date, variant, store=store)
                if k is None:
                    continue
                base = f"gs://{bucket}/{k}"
            path = f"{base}/{INDEX_VARIANTS[variant]}"
            if not exists(path):
                err(f"index-blob: {date} [{variant}] no tier at {path}; skipped")
                continue
            if dry_run:
                err(f"index-blob: {date} [{variant}] would write {groups_parquet_path(path)}{'' if parquet_only or from_json else ' + .groups.json'}")
                continue
            schema, rows = read_groups_blob(path) if from_json else extract(path)
            out, n = write_groups_parquet(path, schema, rows)
            err(f"index-blob: {date} [{variant}] {len(rows)} groups → {out} ({n:,} B)")
            if not (parquet_only or from_json):
                out, n = write_groups_blob(path, schema, rows)
                err(f"index-blob: {date} [{variant}] {len(rows)} groups → {out} ({n:,} B)")


@main.command("index-extras")
@option("-a", "--attribution", "attributions", multiple=True, required=True, help="Attribution parquet(s) (as `path-index -a`)")
@option("-i", "--identities", "identities_path", envvar=IDENTITIES_ENV, required=True, help=f"identities.yaml path or URL (${IDENTITIES_ENV}): the deployment's roster, kept outside the repo")
@option("-o", "--out", "out_dir", type=Path, default=None, help="Where to write attr.tsv (default: beside the index)")
@option("-P", "--path-index", "path_index", type=Path, required=True, help="Floor-free path-index.parquet of the scan (every dir is a row)")
@argument("date")
def index_extras(attributions: tuple[str, ...], identities_path: str, out_dir: Path | None, path_index: Path, date: str) -> None:
    """Backfill a scan's provenance sidecar (`attr.tsv`) from its floor-free
    path index + attribution parquets: each attributing prefix's user /
    source / evidence. `path-index` writes the same file for a fresh scan."""
    import duckdb

    from .extras import write_extras
    from .viz import prefix_labels

    con = duckdb.connect()
    src = f"read_parquet('{path_index}')"
    con.execute(f"CREATE TEMP VIEW idx_dirs AS SELECT DISTINCT path AS fp FROM {src}")
    # Path-glob rules expand against `(bucket, name)` dirs — from the index's own paths.
    con.execute(
        "CREATE TEMP VIEW listing_dirs AS SELECT split_part(fp, '/', 1) AS bucket,"
        " CASE WHEN position('/' IN fp) > 0 THEN substr(fp, position('/' IN fp) + 1) END AS name FROM idx_dirs"
    )
    pfx_df = prefix_labels(con, attributions, identities_path, "listing_dirs")
    counts = write_extras(pfx_df, out_dir or path_index.parent)
    err(f"index-extras {date}: {json.dumps(counts)}")


@main.command("warm-cache")
@option("-d", "--date", help="Scan to warm (default: latest under --root)")
@option("-j", "--jobs", default=4, type=int, help="Concurrent requests (default 4)")
@option("-n", "--dry-run", is_flag=True, help="Print the request paths; fetch nothing")
@option("-r", "--root", help="Snapshots root (default gs://$DATA_BUCKET/snapshots)")
@option("-t", "--token", help="Site read token (default $GCS_USAGE_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base (default gcs.oa.dev)")
@option("-W", "--widths", default="512,1280,1536,1792,1920", help="Canvas widths to warm (the client sends ceil(innerWidth/128)*128; default = phone + common laptops)")
def warm_cache(date: str | None, jobs: int, dry_run: bool, root: str | None, token: str | None, site_url: str | None, widths: str) -> None:
    """Warm the site's subtree + diff caches for a scan: replay the home
    page's default requests (one subtree, the diff span chips 1d/3d/7d/14d/30d
    and the previous-scan pair, each with its summary) at the common canvas
    widths, so the first viewer anywhere gets a cache hit (colo cache + global
    KV). Non-fatal: a failed request just leaves that view cold."""
    from . import warm as wm

    # Deployment config: SITE_URL / SNAPSHOTS_SUBDIR (the CoreWeave job exports
    # cw-s3.oa.dev + snapshots/cw); defaults are the GCS deployment's.
    site_url = site_url or os.environ.get("SITE_URL") or SITE_DEFAULT_URL
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots" + (f"/{os.environ['SNAPSHOTS_SUBDIR'].strip('/')}" if os.environ.get('SNAPSHOTS_SUBDIR') else '')
    dates = wm.scan_dates(root)
    if not dates:
        raise SystemExit("warm-cache: no scans under root")
    date = date or dates[-1]
    if date not in dates:
        raise SystemExit(f"warm-cache: {date} is not a published scan")
    paths = wm.plan(date, dates, tuple(int(w) for w in widths.split(",")))
    if dry_run:
        for p in paths:
            print(p)
        return
    # Auth: an agent bearer token (`-t` / GCS_USAGE_TOKEN — the app gate), or a
    # Cloudflare Access service-token pair (CF_ACCESS_CLIENT_ID/SECRET — a
    # whole-host edge-gated deployment). Same request either way.
    token = secret(token, "GCS_USAGE_TOKEN")
    cid, csec = env_secret("CF_ACCESS_CLIENT_ID"), env_secret("CF_ACCESS_CLIENT_SECRET")
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    elif cid and csec:
        headers = {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}
    else:
        raise SystemExit("warm-cache: need GCS_USAGE_TOKEN (or -t), or CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET")
    res = wm.warm(site_url, headers, paths, jobs=jobs)
    bad = [r for r in res if r[1] != 200]
    err(f"warm-cache: {len(res) - len(bad)}/{len(res)} warmed for {date} in {sum(r[2] for r in res):.0f}s of request time" + (f"; {len(bad)} failed" if bad else ""))


@main.group()
def lifecycle() -> None:
    """Bucket lifecycle rules as a tracked file: `pull` (live → JSON), `diff`
    (file vs live), `push` (file → bucket, whole-config write + read-back
    verification), `gc-rule` (print the S3 bucket-wide noncurrent-version GC
    rule to add to a file). `gs://<bucket>` reads GCS (ADC: the job SA, or
    your gcloud application-default login); a bare name is S3 / CAIOS with the
    keys from the env (see `sweep`). Several `-b` → one JSON map keyed by
    bucket, the per-scan snapshot shape."""


def _lifecycle_clients(buckets: tuple[str, ...]):
    """The S3 and/or GCS client the given bucket URIs need (`None` for a cloud
    none of them name), so one `pull` can snapshot a mixed set."""
    from .lifecycle import is_gcs

    gcs = s3 = None
    if any(is_gcs(b) for b in buckets):
        from google.cloud import storage

        gcs = storage.Client()
    if any(not is_gcs(b) for b in buckets):
        from .sweep import s3_client

        s3 = s3_client()
    return s3, gcs


_LC_BUCKET = option("-b", "--bucket", "buckets", multiple=True, help="`gs://<bucket>` (GCS) or a bare S3 bucket name; repeatable (default $CW_BUCKET)")


def _lc_buckets(buckets: tuple[str, ...]) -> tuple[str, ...]:
    if buckets:
        return buckets
    from .sweep import CW_BUCKET

    return (CW_BUCKET,)


@lifecycle.command("pull")
@_LC_BUCKET
@option("-k", "--keep-going", is_flag=True, help="A bucket whose rules can't be read (e.g. no `storage.buckets.get`) is reported and left out, instead of failing the whole pull — for the job's fleet snapshot")
@option("-o", "--out", type=Path, help="Write here instead of stdout")
def lifecycle_pull(buckets: tuple[str, ...], keep_going: bool, out: Path | None) -> None:
    """One bucket → the bare rule list; several → `{<bucket>: rules}` in this order."""
    from .lifecycle import dump, dump_map, pull_any, pull_many

    buckets = _lc_buckets(buckets)
    s3, gcs = _lifecycle_clients(buckets)
    if len(buckets) == 1:
        text = dump(pull_any(buckets[0], s3=s3, gcs=gcs), bucket=buckets[0])
    else:
        text = dump_map(pull_many(list(buckets), s3=s3, gcs=gcs, keep_going=keep_going))
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text)
        err(f"lifecycle: {', '.join(buckets)} → {out}")


@lifecycle.command("diff")
@_LC_BUCKET
@argument("path", type=Path)
def lifecycle_diff(buckets: tuple[str, ...], path: Path) -> None:
    """Exit 1 when PATH (intended) differs from the live rules of the one -b bucket."""
    from .lifecycle import diff_any, load, pull_any

    (bucket,) = _lc_buckets(buckets)
    s3, gcs = _lifecycle_clients((bucket,))
    d = diff_any(bucket, load(str(path)), pull_any(bucket, s3=s3, gcs=gcs))
    print(json.dumps(d))
    if any(d.values()):
        sys.exit(1)


@lifecycle.command("push")
@_LC_BUCKET
@option("-n", "--dry-run", is_flag=True, help="Print the diff that would be applied; touch nothing")
@argument("path", type=Path)
def lifecycle_push(buckets: tuple[str, ...], dry_run: bool, path: Path) -> None:
    """Replace the one -b bucket's lifecycle configuration with PATH (read back + verified)."""
    from .lifecycle import diff_any, load, pull_any, push_any

    (bucket,) = _lc_buckets(buckets)
    s3, gcs = _lifecycle_clients((bucket,))
    intended = load(str(path))
    base = pull_any(bucket, s3=s3, gcs=gcs)
    d = diff_any(bucket, intended, base)
    if not any(d.values()):
        err(f"lifecycle: {bucket} already matches {path}")
        return
    err(f"lifecycle: {'would apply' if dry_run else 'applying'} to {bucket}: {json.dumps(d)}")
    if dry_run:
        return
    live = push_any(bucket, intended, base=base, s3=s3, gcs=gcs)  # refuses if live moved since the diff
    err(f"lifecycle: {bucket} now has {len(live)} rule(s), verified")


@lifecycle.command("gc-rule")
@option("-d", "--days", default=1, help="NoncurrentDays (1 while versioning is off; the undo window when it's on)")
@option("-p", "--prefix", default="", help="Scope (default: whole bucket)")
def lifecycle_gc_rule(days: int, prefix: str) -> None:
    from .lifecycle import gc_rule

    print(json.dumps(gc_rule(days, prefix), indent=2))


@main.group("plan-sweep")
def plan_sweep() -> None:
    """Plan-first deletion on CoreWeave: build manifests from a plan and execute them (boto3/CAIOS)."""


@plan_sweep.command("manifest")
@option("-d", "--date", required=True, help="Scan id (SNAP_ID) whose layer-2 parquet to pin")
@option("-l", "--l2", "l2_path", help="Layer-2 parquet path (default: /gcs/<data>/cw-l2/<date>/<bucket>.parquet)")
@option("-o", "--out", required=True, help="Output dir for manifest/ + plan-summary.json")
@argument("plan_path")
def plan_sweep_manifest(date: str, l2_path: str | None, out: str, plan_path: str) -> None:
    """Expand a curated PLAN (json) into an object-level deletion manifest.

    Deletes nothing; reads the pinned layer-2 parquet and writes
    manifest/<bucket>.parquet + plan-summary.json under --out."""
    import json

    from .sweep import DATA_BUCKET, build_manifest, load_plan

    plan = load_plan(plan_path)
    if l2_path is None:
        l2_path = f"/gcs/{DATA_BUCKET}/cw-l2/{date}/{plan.bucket}.parquet"
    summary = build_manifest(l2_path, plan, out)
    err(f"manifest: {summary['objects']} objects, {summary['bytes']} bytes -> {summary['manifest']}")
    print(json.dumps(summary))


@plan_sweep.command("execute")
@option("-G", "--no-versioning-guard", is_flag=True, help="Skip the versioning preflight: a real delete is then PERMANENT (no delete marker to undo)")
@option("-r", "--for-real", is_flag=True, help="Actually delete (writes recoverable delete markers); default is a dry run")
@argument("run_dir")
def plan_sweep_execute(no_versioning_guard: bool, for_real: bool, run_dir: str) -> None:
    """Execute the manifest under RUN_DIR against CoreWeave S3 (boto3).

    Default is a dry run (touches nothing). `--for-real` deletes reviewed keys
    whose (size, mtime) still match; refused unless the bucket has versioning
    Status=Enabled — `-G` disables that guard (deletes become permanent; the
    summary records `versioning_guard: false`)."""
    import json

    from .sweep import execute_plan

    s = execute_plan(run_dir, for_real=for_real, require_versioning=not no_versioning_guard)
    err(
        f"{'REAL' if for_real else 'DRY'}: {s['deleted_objects']} objs / {s['deleted_bytes']} bytes; "
        f"gone {s['skipped_gone']} overwritten {s['skipped_overwritten']} "
        f"drift {s['drift_new']} failed {s['delete_failed']}"
    )
    print(json.dumps(s))


@plan_sweep.command("undo")
@option("-n", "--dry-run", is_flag=True, help="Report what would be restored without touching anything")
@option("-p", "--prefix", "prefixes", multiple=True, help="Restrict undo to keys under this prefix (repeatable)")
@argument("run_dir")
def plan_sweep_undo(dry_run: bool, prefixes: tuple[str, ...], run_dir: str) -> None:
    """Undo a real run under RUN_DIR: remove its delete markers (recoverable
    delete). Must run before `purge`."""
    import json

    from .sweep import undo_run

    s = undo_run(run_dir, prefixes=list(prefixes) or None, dry_run=dry_run)
    err(f"{'DRY ' if dry_run else ''}undo: restored {s['restored']} (failed {s['restore_failed']}, skipped {s['skipped']})")
    print(json.dumps(s))


@plan_sweep.command("purge")
@option("-n", "--dry-run", is_flag=True, help="Report what would be purged without touching anything")
@argument("run_dir")
def plan_sweep_purge(dry_run: bool, run_dir: str) -> None:
    """Permanently drop every version of a real run's deleted keys under RUN_DIR
    — the irreversible space-reclaim stage, after the undo hold."""
    import json

    from .sweep import purge_run

    s = purge_run(run_dir, dry_run=dry_run)
    err(f"{'DRY ' if dry_run else ''}purge: {s['purged_versions']} versions / {s['purged_bytes']} bytes (failed {s['purge_failed']})")
    print(json.dumps(s))


@main.group()
def sweep() -> None:
    """The GCS executor's phases — manifest / execute / undo (specs/staged-delete.md)."""


@sweep.command("manifest")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets (default: every bucket the plan names)")
@option("-d", "--date", required=True, help="Scan date whose listing to plan from (pinned)")
@option("-o", "--out", default=None, help="Output dir (default gs://oa-gcs-usage-dvx/sweep/<date>-p<plan_id>)")
@option("-p", "--plan", "plan_path", required=True, help="The dispatched plan.json (path or gs:// URL): its items are the delete set, and the buckets are the plan's (∩ -b)")
@option("-r", "--root", default="gs://oa-gcs-usage-dvx", help="Listing root (gs:// or local mount)")
def sweep_manifest(only_buckets: tuple[str, ...], date: str, out: str | None, plan_path: str, root: str) -> None:
    """Object-level manifest of a staged plan (specs/staged-delete.md): stream
    the pinned listing and write per-bucket parquets of the ELIGIBLE keys —
    every key under a staged prefix — plus a category summary. The plan is
    the whole intent: nothing carves out. Pure read + artifact write —
    deletes nothing."""
    import fsspec
    import pyarrow as pa
    import pyarrow.parquet as pq

    from .staged_plan import CATEGORIES, load_plan

    sp = load_plan(plan_path)
    buckets = [b for b in sp.buckets if not only_buckets or b in only_buckets]
    if not buckets:
        raise SystemExit(f"no plan bucket among -b {', '.join(only_buckets)} (plan {sp.plan_id} names {', '.join(sp.buckets)})")
    out = out or f"gs://oa-gcs-usage-dvx/sweep/{date}-p{sp.plan_id}"
    err(f"sweep manifest: scan {date} from plan {sp.plan_id} ({sp.name!r}) → {out}"
        + f" · {sum(len(sp.sweep[b]) for b in buckets)} staged prefix(es) on {', '.join(buckets)}")
    # The staged prefixes are the run's bands: `sweep execute` lists one
    # segment below each and accounts per band, so every `deletion_bands` row
    # is one staged item.
    summary: dict = {
        "date": date, "plan_id": sp.plan_id, "plan_name": sp.name,
        "approved": [a for b in buckets for a in sp.bands(b)],
        "buckets": {},
    }

    fs, rootpath = fsspec.core.url_to_fs(root)
    schema = pa.schema([
        ("name", pa.string()), ("size_bytes", pa.int64()),
        ("storage_class_id", pa.int8()), ("created", pa.timestamp("us", tz="UTC")),
        ("dir", pa.string()),
    ])
    for bucket in buckets:
        shards = sorted(fs.glob(f"{rootpath}/listing/{date}/{bucket}/*.parquet"))
        if not shards:
            raise SystemExit(f"no listing shards for {bucket} under {root}/listing/{date}/")
        cache: dict[str, str] = {}
        cats = {c: [0, 0] for c in CATEGORIES}  # bytes, objects
        bands = sp.sweep[bucket]
        writer = None
        out_path = f"{out}/manifest/{bucket}.parquet"
        ofs, opath = fsspec.core.url_to_fs(out_path)
        ofs.makedirs(opath.rsplit("/", 1)[0], exist_ok=True)
        n = 0
        for shard in shards:
          # Open/close each shard deterministically: a gcsfs file left for the
          # interpreter's exit to finalize calls into fsspec's event loop while
          # it is tearing down and can hang the process forever (observed
          # 2026-09-08: the manifest step's last line printed, then 0% CPU for
          # an hour and `sweep execute` never started).
          with fs.open(shard, "rb") as fh:
            pf = pq.ParquetFile(fh)
            for batch in pf.iter_batches(columns=["name", "size_bytes", "storage_class_id", "created"], batch_size=1 << 17):
                df = batch.to_pandas()
                n += len(df)
                inb = df["name"].str.startswith(bands)
                if not inb.all():
                    cats["outside_bands"][0] += int(df["size_bytes"][~inb].sum())
                    cats["outside_bands"][1] += int((~inb).sum())
                    df = df[inb]
                    if df.empty:
                        continue
                dirs = df["name"].str.rpartition("/")[0]
                for dn in dirs.unique():
                    if dn not in cache:
                        cache[dn] = sp.classify(bucket, dn)
                cat = dirs.map(lambda dn: cache[dn])
                sizes = df["size_bytes"]
                for c, g in sizes.groupby(cat):
                    cats[c][0] += int(g.sum())
                    cats[c][1] += len(g)
                elig = cat == "eligible"
                if elig.any():
                    sel = df[elig].copy()
                    sel["dir"] = dirs[elig]
                    t = pa.Table.from_pandas(sel, preserve_index=False).select(schema.names).cast(schema)
                    if writer is None:
                        writer = pq.ParquetWriter(opath, schema, filesystem=ofs)
                    writer.write_table(t)
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
    _hard_exit()


@sweep.command("execute")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("-D", "--drift", type=Choice(["skip", "proceed"]), default="skip", help="Dirs that gained new keys since the scan: skip (default) or proceed (manifest keys only — new keys always survive)")
@option("-w", "--workers", default=8, type=int, help="Concurrent directory re-lists")
@option("-W", "--delete-workers", default=32, type=int, help="Concurrent delete batches (100 objects each), shared by every re-list; the bucket's ~1000 writes/s is the ceiling")
@option("--for-real", is_flag=True, help="Actually delete (default: dry-run writes would-delete/)")
@option("--no-record", is_flag=True, help="Skip the D1 deletion_runs/bands record (recorded by default)")
@argument("plan_dir")
def sweep_execute(only_buckets: tuple[str, ...], drift: str, delete_workers: int, workers: int, for_real: bool, no_record: bool, plan_dir: str) -> None:
    """Execute (default: DRY-RUN) a `sweep manifest` plan: fresh re-list per
    eligible dir, generation-matched deletes of manifest∩live keys whose
    timeCreated is unchanged. The plan is the whole intent: nothing is
    re-classified. `--for-real` additionally requires ≥7d soft delete on every
    bucket."""
    import fsspec

    from .sweep_exec import DELETE_ATTEMPTS, execute_plan

    DELETE_ATTEMPTS_NOTE = f"{DELETE_ATTEMPTS} attempts"
    with fsspec.open(f"{plan_dir}/plan-summary.json") as fh:
        plan_summary = json.load(fh)
    err(f"execute {'FOR REAL' if for_real else '(dry-run)'} plan {plan_summary['plan_id']} ({plan_summary.get('plan_name')!r})")

    started = int(dt.datetime.now(dt.timezone.utc).timestamp())
    actor = os.environ.get("USER", "?")
    if not no_record:
        # The run's D1 row goes in now (finished NULL) so /staged lists it while
        # the re-list runs — hours, on the big bands; completed at the end.
        from .sweep_exec import record_run_start
        try:
            run_id = record_run_start(plan_summary, plan_dir, actor=actor, started_ts=started, for_real=for_real, buckets=only_buckets)
            err(f"recorded deletion run {run_id} (in progress)")
        except Exception as e:  # recording must never block the run
            err(f"WARN: deletion-run start record failed: {e}")
    # A clean stop: `sweep stop PLAN` drops PLAN/STOP (polled every 10 s), or
    # SIGTERM — roots not yet started are left for a re-run, everything done
    # is logged and recorded, and the job ends red (exit 130).
    import signal
    import threading
    from .sweep_exec import stop_file_watch
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop_file_watch(plan_dir, stop)
    summary = execute_plan(
        plan_dir,
        for_real=for_real,
        only_buckets=only_buckets,
        drift=drift,
        workers=workers,
        delete_workers=delete_workers,
        stop=stop,
    )
    finished = int(dt.datetime.now(dt.timezone.utc).timestamp())
    total = sum(b.get("delete_bytes", 0) for b in summary["buckets"].values())
    err(f"\n{'deleted' if for_real else 'would delete'}: {total / 1e12:.2f} TB total")
    failed = {b: v["failed_dirs"] for b, v in summary["buckets"].items() if v.get("failed_dirs")}
    if not no_record:
        from .sweep_exec import record_run
        # The undo deadline follows the narrowest window actually measured on
        # the run's buckets (the guard already refused anything under 7 d).
        windows = [int(v["soft_delete_days"]) for v in summary["buckets"].values() if "soft_delete_days" in v]
        try:
            run_id = record_run(summary, summary["_plan"], actor=actor, started_ts=started, finished_ts=finished, soft_delete_days=min(windows) if windows else 7)
            err(f"recorded deletion run {run_id}")
        except Exception as e:  # recording must never mask a completed run
            err(f"WARN: deletion-run record failed: {e}")
    if stop.is_set():
        skipped = sum(v.get("interrupted", {}).get("roots_skipped", 0) for v in summary["buckets"].values())
        err(f"STOPPED: {skipped:,} listing root(s) not started — re-run the plan to finish (done keys resolve as skipped_gone)")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(130)
    if failed:
        # Logged and recorded above; the job still ends red so nobody reads
        # "succeeded" over deletes GCS never answered for.
        n = sum(d["objects"] for ds in failed.values() for d in ds)
        err(f"ERROR: {n:,} delete(s) in {sum(map(len, failed.values())):,} dir(s) got no definitive answer after {DELETE_ATTEMPTS_NOTE} — see `failed_dirs` in the summary; a re-run settles them (already-gone → skipped_gone)")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(2)
    _hard_exit()


@sweep.command("stop")
@argument("plan_dir")
def sweep_stop(plan_dir: str) -> None:
    """Ask a running `sweep execute` on PLAN_DIR to stop cleanly: drops
    PLAN_DIR/STOP, which the executor polls every 10 s — roots not yet
    started are left for a re-run, everything done is logged and recorded."""
    import fsspec
    with fsspec.open(f"{plan_dir}/STOP", "w") as fh:
        fh.write(dt.datetime.now(dt.timezone.utc).isoformat())
    err(f"wrote {plan_dir}/STOP")
    _hard_exit()


@sweep.command("undo")
@option("-b", "--bucket", "only_buckets", multiple=True, help="Only these buckets")
@option("-n", "--dry-run", is_flag=True, help="List what would be restored; call nothing")
@option("-p", "--prefix", "prefixes", multiple=True, help="Only objects under these prefixes (gs://bucket/dir/); default: everything the run deleted")
@option("-w", "--workers", default=16, type=int, help="Concurrent restore calls")
@option("--no-record", is_flag=True, help="Skip the D1 undo_state / undone_objects update")
@argument("run")
def sweep_undo(only_buckets: tuple[str, ...], dry_run: bool, prefixes: tuple[str, ...], workers: int, no_record: bool, run: str) -> None:
    """Restore what a real run deleted, from its `deleted/` logs — the
    soft-delete restore of exactly the logged generations, valid until the
    run's `undo_deadline` (finish + the buckets' 7-day window). RUN is the D1
    run id (`<scan>-p<plan_id>/<utc stamp>`, as /staged lists it) or the run's
    gs:// log dir. Re-runnable: names already live again are left alone."""
    from .index_footer import _creds, _d1_query, _q
    from .sweep_exec import record_undo, undo_run

    row = None
    try:
        tok, acct = _creds()
        rows = _d1_query(
            "SELECT run_id, mode, undo_deadline, undo_state, log_dir, deleted_objects FROM deletion_runs "
            f"WHERE run_id = {_q(run)} OR log_dir = {_q(run)}", acct, tok,
        )
        row = rows[0] if rows else None
    except Exception as e:
        if not run.startswith("gs://"):
            raise SystemExit(f"D1 lookup failed and RUN is not a gs:// log dir: {e}")
        err(f"WARN: D1 lookup failed ({e}); proceeding on the log dir alone (no deadline check, no record)")
    if row is None and not run.startswith("gs://"):
        raise SystemExit(f"no deletion run {run!r} in D1 (see /staged for run ids)")
    if row is not None and row["mode"] != "real":
        raise SystemExit(f"{row['run_id']} was a dry run — nothing to undo")
    log_dir = row["log_dir"] if row is not None else run
    deadline = row.get("undo_deadline") if row is not None else None
    if row is not None:
        err(f"undo {row['run_id']}: {row['deleted_objects']:,} deleted objects, undo_state={row['undo_state']}, "
            f"window until {dt.datetime.fromtimestamp(deadline, dt.timezone.utc):%Y-%m-%d %H:%MZ}" if deadline else f"undo {row['run_id']}")
    summary = undo_run(log_dir, only_buckets=only_buckets, prefixes=prefixes, dry_run=dry_run, workers=workers, deadline=deadline)
    if row is not None and not no_record and not dry_run:
        try:
            err(f"recorded undo_state={record_undo(row['run_id'], summary, int(row['deleted_objects'] or 0))}")
        except Exception as e:  # recording must never mask a completed undo
            err(f"WARN: undo record failed: {e}")
    _hard_exit()


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


@main.command("export")
@option("-d", "--date", default=None, help="Scan date YYYY-MM-DD[THHMM] (default: the newest in the store's scans.json)")
@option("-e", "--executor", default=None, type=Choice(["sweep", "plan-sweep"]), help="`runs` only: the site's executor route family (`Store.executor`: gcs `sweep`, cw `plan-sweep`)")
@option("-l", "--list", "list_sources", is_flag=True, help="Print the sources and their columns, and exit")
@option("-o", "--out", default="-", help="CSV output path (default: stdout)")
@option("-s", "--subdir", default=None, help="Snapshot subdir under /data/ for scans.json (default: $SNAPSHOTS_SUBDIR; `cw` on cw-s3)")
@option("-t", "--token", default=None, help="Bearer token (default: $GCS_USAGE_TOKEN)")
@option("-u", "--url", default=None, help=f"Site base URL (default: $GCS_USAGE_URL or {SITE_DEFAULT_URL})")
@option("-U", "--unit", default="B", type=Choice(["B", "GiB", "TiB"]), help="Byte columns as raw bytes (default) or rounded GiB / TiB, header `<col> (<unit>)`")
@argument("source", required=False)
def export_cmd(date: str | None, executor: str | None, list_sources: bool, out: str, subdir: str | None, token: str | None, url: str | None, unit: str, source: str | None) -> None:
    """Export one named SOURCE from the live site API as a CSV with a fixed
    column contract (`--list` shows them) — the input `sheet-push -k` mirrors
    into a Google Sheet tab. See specs/done/sheet-mirror.md."""
    from .sheet_mirror import ExportArgs, export, list_sources as sources_lines, write_csv  # noqa: PLC0415
    from .site import creds, get_json  # noqa: PLC0415

    if list_sources:
        print("\n".join(sources_lines()))
        return
    if not source:
        raise SystemExit("export: SOURCE required (see `dt-cloud export --list`)")
    base, tok = creds(token, url)
    if not tok:
        raise SystemExit("export: no token (-t or $GCS_USAGE_TOKEN)")
    args = ExportArgs(date=date, executor=executor, subdir=subdir if subdir is not None else (env_secret("SNAPSHOTS_SUBDIR") or ""), unit=unit)
    columns, rows = export(source, lambda path, params: get_json(base, tok, path, params), args)
    if out == "-":
        write_csv(columns, rows, sys.stdout)
    else:
        with open(out, "w", newline="") as fh:
            write_csv(columns, rows, fh)
    err(f"{source}: {len(rows)} rows → {out}")


@main.command("sheet-push")
@option("-c", "--create", is_flag=True, help="create the tab if the sheet has none by that title (header row frozen + bold, columns sized to the first fill)")
@option("-D", "--disclaimer", help="static footer text 2 rows below the table; a '; last change <ts>' stamp is appended that only advances when data changes")
@option("-I", "--impersonate", help="service-account email to impersonate for Sheets auth (needs Token Creator); default is ambient ADC")
@option("-k", "--key", default=None, help="stable row identity column: existing rows keep their order, new keys append, removed keys clear (compacted on an otherwise-unchanged run); default positional")
@option("-n", "--dry-run", is_flag=True, help="parse + summarize, don't touch the sheet")
@option("-w", "--worksheet", required=True, help="tab to sync, by title (never the first tab by default: the sheet may hold human-authored tabs)")
@argument("sheet_id")
@argument("csv_path", default="-")
def sheet_push(create: bool, disclaimer: str | None, impersonate: str | None, key: str | None, dry_run: bool, worksheet: str, sheet_id: str, csv_path: str) -> None:
    """Push a CSV (header + rows, e.g. from `export`) into one named tab of a
    Google Sheet — the generic CSV → tab writer behind the sheet mirror.

    Syncs ONE named tab in place (`-w <title>`); other tabs (derived views
    people add) are untouched. Writes only the cells whose value actually
    changed (diffing the tab's current contents, numerically where possible),
    so Google's Version History highlights just the real deltas — and
    formatting / frozen rows survive. With `-k <column>` the diff is by key,
    not position: an added row is one new row at the end, a removed row one
    cleared row (holes are compacted on a later run whose data is otherwise
    unchanged). `-D` writes an "auto-synced" footer two rows below the table,
    whose "last change" stamp only advances when data moves — so a no-op run
    writes nothing. Idempotent.

    Auth is Application Default Credentials: the job's GCP service account in
    Cloud Run, or your `gcloud auth application-default` locally. The sheet
    must be shared (Editor) with that identity, and the Sheets API enabled in
    the project. `-c` creates a missing tab (appended last, header frozen).

        dt-cloud export owners -o owners.csv && dt-cloud sheet-push -k user -w 'Storage by user' <id> owners.csv
    """
    import csv
    import datetime
    import io as _io

    from .sheet_mirror import plan_sheet, push  # noqa: PLC0415

    text = sys.stdin.read() if csv_path == "-" else Path(csv_path).read_text()
    rows = [r for r in csv.reader(_io.StringIO(text)) if r]
    if not rows:
        raise SystemExit("expected a CSV with at least a header row, got nothing")
    if key and key not in rows[0]:
        raise SystemExit(f"-k {key!r} is not a column of {rows[0]}")
    err(f"{len(rows) - 1} rows → sheet {sheet_id} tab '{worksheet}'{f' by {key!r}' if key else ''}")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if dry_run:
        plan_sheet([], rows, now, key=key, disclaimer=disclaimer)  # validates keys (dupes/empties)
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
    sheet = gspread.authorize(creds).open_by_key(sheet_id)
    created = False
    try:
        ws = sheet.worksheet(worksheet)
    except gspread.WorksheetNotFound:
        if not create:
            raise SystemExit(f"sheet {sheet_id} has no tab {worksheet!r} (-c creates it)") from None
        ws = sheet.add_worksheet(worksheet, rows=len(rows) + 10, cols=len(rows[0]))
        ws.freeze(rows=1)
        ws.format("1:1", {"textFormat": {"bold": True}})
        created = True
        err(f"created tab '{worksheet}'")
    plan = push(ws, rows, now, key=key, disclaimer=disclaimer, cell=gspread.Cell)
    if created:
        # Width from the table only (auto-resize would stretch column A to the
        # footer); set once, so later width tweaks by people stick.
        sheet.batch_update({"requests": [
            {"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": 7 * max(len(r[i]) for r in rows if i < len(r)) + 24},
                "fields": "pixelSize",
            }}
            for i in range(len(rows[0]))
        ]})
    verb = "changed" if plan.data_changed else ("unchanged, compacted" if plan.compacted else "unchanged")
    holes = f", {plan.holes} cleared row(s) held for compaction" if plan.holes else ""
    err(f"synced '{ws.title}': {len(plan.cells)} cell(s) written ({plan.data_rows} data rows, data {verb}{holes})")


@main.group("sheet-mirror")
def sheet_mirror() -> None:
    """A deployment's `sheet-mirror.yml` → what `deploy/sheet-mirror/` runs."""


@sheet_mirror.command("env")
@argument("config")
def sheet_mirror_env(config: str) -> None:
    """Print the deploy variables (SITE, TOKEN_SECRET, SCHEDULE, PROJECT,
    REGION, SA, JOB, TRIGGER, IMAGE) as shell-quoted `KEY=value` lines, for
    `build.sh` / `deploy.sh` to `eval`. CONFIG is a path, or `-` for stdin."""
    from .sheet_mirror import env_lines, read_config  # noqa: PLC0415

    print("\n".join(env_lines(read_config(config))))


@sheet_mirror.command("render")
@argument("config")
def sheet_mirror_render(config: str) -> None:
    """Print CONFIG with every `${NAME}` substituted from the environment, as
    YAML — validated first; an unset NAME is an error. `deploy.sh` bakes this
    into the job, so an id kept out of the repo (e.g. `sheet: ${GCS_SHEET_ID}`,
    set in an untracked `.envrc`) reaches the job but never git."""
    from .sheet_mirror import render_config  # noqa: PLC0415
    print(render_config(sys.stdin.read() if config == "-" else Path(config).read_text()), end="")


@sheet_mirror.command("plan")
@argument("config")
def sheet_mirror_plan(config: str) -> None:
    """Validate CONFIG and print one line per mirror — shell-quoted
    `source= site= subdir= sheet= tab= key= footer= executor= unit=` assignments —
    for `sync.sh` to `eval` in its loop. CONFIG is a path, or `-` for stdin."""
    from .sheet_mirror import plan_lines, read_config  # noqa: PLC0415

    print("\n".join(plan_lines(read_config(config))))


@main.command("cascade-a2a")
@option("-b", "--bucket", required=True, help="Bucket the DT tier was imported as (its rows are relative to it)")
@option("-i", "--index", "index_path", required=True, help="mgu floor-free path-index parquet (`path, depth, usr, b, o, wts, wb, c2, c3, c4, a`)")
@option("-j", "--json", "as_json", is_flag=True, help="Machine-readable report on stdout")
@option("-n", "--top", default=10, help="Examples per mismatch class")
@argument("dirs_tier")
def cascade_a2a(bucket: str, index_path: str, as_json: bool, top: int, dirs_tier: str) -> None:
    """The A.3 gate (spec specs/done/mgu-scale-unification.md): DT's `import -e duckdb
    --label usr` dirs tier against mgu's path index for one bucket, joined on
    `(path, usr)` — rows only one side has, and per-column disagreements
    (`b`↔`size`, `o`↔`n_files`, `c2..c4`↔`sum_storage_class_id_*`,
    `wts/wb`↔`mtime_mean`). Exit 1 on any difference."""
    from .cascade_a2a import compare, render

    report = compare(bucket, index_path, dirs_tier, top)
    print(json.dumps(report, indent=1, default=str) if as_json else render(report))
    if not report["ok"]:
        raise SystemExit(1)


def _icons_dir(rel: str = "job/icons") -> Path:
    """``rel`` (e.g. `job/icons`) in both layouts: pip-installed in the job image
    (cwd=/app → /app/job/icons) or the repo checkout (…/parents[3]/job/icons)."""
    cands = (Path.cwd() / rel, Path(__file__).resolve().parents[3] / rel)
    return next((c for c in cands if c.exists()), cands[-1])


def _digest_options(template_default: str):
    """`digest`'s options; `cw-digest` is the same command defaulting to `-T cw`."""
    opts = [
        option("-b", "--bot-token", help="Discord bot token: opens the month's thread + resolves app emoji (default $DISCORD_BOT_TOKEN; with -P discord)"),
        option("-c", "--channel", help="Slack channel id (default $SLACK_CHANNEL)"),
        option("-C", "--config", "config_path", type=Path, help="YAML/JSON DigestConfig overlaid on the template's preset (title, site, root, state, plot host, buckets/quotas, prices…; see specs/digest-unification.md)"),
        option("-D", "--reply-delay", "reply_delay", default=0.0, type=float, help="Seconds to sleep between replies (e.g. 305 for a spaced Slack backfill so per-reply sender chrome survives; Discord needs none)"),
        option("-E", "--edit-replies", is_flag=True, help="Re-edit every already-posted reply to its current body (backfill after a format change; -P discord only)"),
        option("-F", "--for-real", is_flag=True, help="With --redo-replies: actually post the new replies and delete the old ones (default: print the plan)"),
        option("-H", "--reply-hour", type=int, default=None, help="cw template: UTC hour the sender variant's daily reply is taken from — the day's first scan at/after it (default 12 → the 12:01Z morning scan, 8:01 am ET; the day's other scans still feed the OP + plot, and with the config's `provisional: true` its earlier ones post a provisional reply)"),
        option("-i", "--icons-dir", type=Path, default=None, help="Where the plot PNG is written + deployed from (default the config's `icons_dir`: job/icons, job/icons-cw)"),
        option("-m", "--month", help="Month YYYY-MM (default: current UTC month)"),
        option("-n", "--dry-run", is_flag=True, help="Render the plot + print OP/replies; post & host nothing"),
        option("-P", "--platform", type=Choice(["slack", "discord"]), default="slack", help="Which thread to converge (default slack)"),
        option("-r", "--root", help="Snapshots root (default the config's: gs://$DATA_BUCKET/snapshots[/cw])"),
        option("-R", "--redo-replies", is_flag=True, help="Re-post the month's replies under the current day rule, then delete the old ones (Slack; dry-run unless --for-real)"),
        option("-t", "--token", help="Slack bot token (default $SLACK_BOT_TOKEN)"),
        option("-T", "--template", type=Choice(["gcs", "cw"]), default=template_default, help=f"Post style + preset config (default {template_default}): gcs = a reply per scan, $/mo by storage class, class mosaic; cw = a reply per day, % of quota per bucket, quota sparkline + diff treemap"),
        option("-u", "--url", "site_url", default=None, help="Site base for links (default the config's: gcs.oa.dev, cw-s3.oa.dev)"),
        option("-V", "--variant", type=Choice(["sender", "body"]), default=None, help="Reply style (cw template): headline as the sender name, posted once from the day's morning scan (sender, default) or bold in the body, edited as the day's scans land (body)"),
        option("-w", "--webhook", help="Discord webhook URL in the digest channel (default $<config discord_webhook_env>: gcs $DISCORD_GCS_USAGE_WEBHOOK, cw none; with -P discord)"),
    ]

    def deco(f):
        for o in reversed(opts):
            f = o(f)
        return f
    return deco


def _digest(
    bot_token: str | None,
    channel: str | None,
    config_path: Path | None,
    reply_delay: float,
    edit_replies: bool,
    for_real: bool,
    reply_hour: int | None,
    icons_dir: Path | None,
    month: str | None,
    dry_run: bool,
    platform: str,
    root: str | None,
    redo_replies: bool,
    token: str | None,
    template: str,
    site_url: str | None,
    variant: str | None,
    webhook: str | None,
) -> None:
    from dataclasses import replace

    from . import digest as dg

    cfg = dg.load_config(template, config_path)
    cfg = replace(cfg, site_url=site_url or cfg.site_url, reply_hour=cfg.reply_hour if reply_hour is None else reply_hour)
    tpl = dg.template(cfg)
    variant = variant or cfg.variant
    if variant not in tpl.variants:
        raise SystemExit(f"digest: the {cfg.template} template has no {variant!r} variant ({' | '.join(tpl.variants)})")
    m = (
        dt.datetime.strptime(month, "%Y-%m").date()
        if month
        else dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    )
    root = root or cfg.resolve_root()

    if dry_run:
        print(dg.dry_run(tpl, root, m, variant))
        return

    if platform == "discord":
        if redo_replies:
            raise SystemExit("digest: -R/--redo-replies is Slack-only")
        webhook = secret(webhook, cfg.discord_webhook_env) if cfg.discord_webhook_env else webhook
        bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
        if not (webhook and bot_token):
            raise SystemExit(f"digest: -P discord needs {cfg.discord_webhook_env or 'a webhook (-w)'} + DISCORD_BOT_TOKEN (or -w/-b)")
        dg.post_digest_discord(tpl, root, m, webhook, bot_token, edit_replies=edit_replies)
        err(f"digest: converged {m:%Y-%m} (discord)")
        return
    if edit_replies:
        raise SystemExit("digest: -E/--edit-replies is Discord-only (Slack replies are re-posted with -R/--redo-replies)")
    channel = channel or os.environ.get("SLACK_CHANNEL")
    token = secret(token, "SLACK_BOT_TOKEN")
    if not (channel and token):
        raise SystemExit("digest: need SLACK_BOT_TOKEN + SLACK_CHANNEL (or -t/-c)")
    from thrds.slack import SlackClient

    client = SlackClient(token, channel)
    icons = icons_dir or _icons_dir(cfg.icons_dir)

    def deploy(local: Path, name: str) -> str | None:
        # publish the icons dir (incl. the freshly-rendered plot) to the
        # config's Pages project + branch — cw's is a preview branch, never the
        # production branch whose root alias serves the arrow avatars
        return dg.pages_deploy(icons, cfg.plot_project, cfg.plot_branch)

    if redo_replies:
        # rule change: re-post every reply under the current day rule, then retire the old ones
        plan = dg.redo_replies(tpl, root, m, client, channel, variant, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay, for_real=for_real)
        if for_real:
            err(f"digest: re-threaded {m:%Y-%m} ({variant}): {len(plan.get('posted', {}))} replies" + (f", {len(plan['stale'])} old left undeleted" if plan.get("stale") else ""))
            return
        old = {day: e for day, e in plan["old"]}
        print(f"digest --redo-replies {m:%Y-%m} in {channel} ({variant}; dry-run — -F/--for-real applies):")
        print(f"  old replies to delete: {len(plan['old'])}")
        for day, e in plan["old"]:
            print(f"    {day}  {e['scan'] if isinstance(e, dict) else day}  ts={e['ts'] if isinstance(e, dict) else e}")
        print(f"  new replies to post: {len(plan['new'])}")
        for day, scan, head in plan["new"]:
            same = "  (same scan as the old reply)" if isinstance(old.get(day), dict) and old[day]["scan"] == scan else ""
            print(f"    {day}  {scan}  {head!r}{same}")
        return
    # the scheduled run (no -m) also closes the previous month's thread if its last day's provisional reply is still up
    converge = dg.converge_slack if month else dg.converge_slack_now
    converge(tpl, root, m, client, channel, variant, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay)
    err(f"digest: converged {m:%Y-%m} ({cfg.template}, {variant})")


@main.command()
@_digest_options("gcs")
def digest(**kw) -> None:
    """Converge the monthly digest thread: an OP (month-to-date headline,
    per-week bullets, plot) edited in place + one reply per scan (gcs template)
    or per UTC day (cw), in Slack (default) or its Discord twin (`-P discord`:
    webhook OP with the plot attached, bot-opened thread, webhook replies).
    State beside the snapshots, per the config's `state`/`discord_state`
    (gcs: digest/<YYYY-MM>.json; cw: digest/cw/<channel>/<variant>/<YYYY-MM>.json).
    See specs/digest-unification.md."""
    _digest(**kw)


@main.command("cw-digest")
@_digest_options("cw")
def cw_digest(**kw) -> None:
    """`digest -T cw` (kept so the cw job's invocation is unchanged)."""
    _digest(**kw)


if __name__ == "__main__":
    main()


@main.command("discord-emoji")
@option("-b", "--bot-token", help="Discord bot token (default $DISCORD_BOT_TOKEN)")
@option("-i", "--icons", type=Path, help="Dir of arrow_deg*.png glyphs (default job/icons/arrows)")
@option("-n", "--dry-run", is_flag=True, help="Say what would be uploaded; upload nothing")
def discord_emoji(bot_token: str | None, icons: Path | None, dry_run: bool) -> None:
    """Upload the digest's trend-arrow glyphs (arrow_deg-80 … arrow_deg80) as
    application emoji on the bot, so `digest -P discord` can render `:arrow_degN:`
    as `<:arrow_degN:id>` (negatives become `arrow_degmN`: Discord names allow no
    `-`). Idempotent: names already on the app are kept. Prints `name id` for the
    whole set on stdout."""
    import re

    from . import digest as dg
    from . import discord_api as api

    bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
    if not bot_token:
        raise SystemExit("discord-emoji: need DISCORD_BOT_TOKEN (or -b)")
    icons = icons or _icons_dir() / "arrows"
    glyphs = {
        dg.emoji_name(int(m.group(1))): p
        for p in sorted(icons.glob("arrow_deg*.png"))
        if (m := re.fullmatch(r"arrow_deg(-?\d+)\.png", p.name))
    }
    if not glyphs:
        raise SystemExit(f"discord-emoji: no arrow_deg*.png under {icons}")
    app = api.app_id(bot_token)
    have = api.app_emojis(bot_token, app)
    for name, p in glyphs.items():
        if name in have:
            continue
        if dry_run:
            err(f"would upload {name} <- {p.name}")
            continue
        have[name] = api.upload_app_emoji(bot_token, app, name, p)
        err(f"uploaded {name} <- {p.name}")
    for name in sorted(have):
        print(name, have[name])


@main.command("discord-webhook")
@option("-b", "--bot-token", help="Discord bot token (default $DISCORD_BOT_TOKEN)")
@option("-c", "--channel", required=True, help="Channel id, or `#name` resolved in --guild")
@option("-g", "--guild", help="Guild id for a `#name` channel (default $DISCORD_GUILD)")
@option("-N", "--name", default="GCS usage", help="Webhook name (default 'GCS usage')")
def discord_webhook(bot_token: str | None, channel: str, guild: str | None, name: str) -> None:
    """Create (or reuse, by name) a webhook owned by the bot's application in a
    channel, and print its URL on stdout. App-owned matters: Discord renders the
    bot's application emoji (`discord-emoji`) only from the bot or a webhook the
    bot owns — through a user-created webhook `<:name:id>` silently degrades to
    `:name:`. Needs Manage Webhooks on the channel. The URL embeds a secret:
    redirect stdout into a secret store or a 0600 file, never a log."""
    from . import discord_api as api

    bot_token = secret(bot_token, "DISCORD_BOT_TOKEN")
    if not bot_token:
        raise SystemExit("discord-webhook: need DISCORD_BOT_TOKEN (or -b)")
    if channel.startswith("#"):
        guild = guild or os.environ.get("DISCORD_GUILD")
        if not guild:
            raise SystemExit("discord-webhook: a `#name` channel needs -g/--guild (or $DISCORD_GUILD)")
        chans = api.guild_channels(bot_token, guild)
        if channel[1:] not in chans:
            raise SystemExit(f"discord-webhook: no text channel {channel} in guild {guild}")
        channel = chans[channel[1:]]
    app = api.app_id(bot_token)
    mine = [h for h in api.channel_webhooks(bot_token, channel) if h.get("application_id") == app and h["name"] == name]
    if mine:
        hook = mine[0]
        err(f"discord-webhook: reusing app-owned webhook {hook['id']} ({name!r}) in channel {channel}")
    else:
        hook = api.create_webhook(bot_token, channel, name)
        err(f"discord-webhook: created app-owned webhook {hook['id']} ({name!r}) in channel {channel}")
    print(api.webhook_url(hook))


@main.command("publish-r2")
@option("-a", "--all-gens", is_flag=True, help="Copy every `index/<gen>/` under the scan, pointed or not (no D1 read); default: only the generations a D1 `index_schema` row names")
@option("-b", "--bucket", "src_bucket", default=None, help="Source GCS scan store (default $DATA_BUCKET)")
@option("-l", "--layer2", default=None, help="Layer-2 dir template, `{scan}` = the scan id (default $LAYER2_PREFIX, else listing/{scan}/index/; cw: cw-l2/{scan}/)")
@option("-L", "--no-listings", is_flag=True, help="Leave the canonical per-bucket listings (`<layer-2 dir>/<bucket>.parquet`) in GCS only; copy the tiers + snapshot JSONs")
@option("-n", "--dry-run", is_flag=True, help="List the keys that would be copied; copy nothing")
@option("-p", "--prefix", "prefixes", multiple=True, help="Key prefix to publish (repeatable; default: the scan's served subset — snapshots/<subdir>/<scan>/ + the layer-2 dir)")
@option("-s", "--subdir", default=None, help="Snapshots subdir of this store (default $SNAPSHOTS_SUBDIR, else none)")
@option("-w", "--workers", default=8, type=int, help="Concurrent HEADs/uploads (default 8)")
@argument("scan")
def publish_r2(
    all_gens: bool,
    src_bucket: str | None,
    layer2: str | None,
    no_listings: bool,
    dry_run: bool,
    prefixes: tuple[str, ...],
    subdir: str | None,
    workers: int,
    scan: str,
) -> None:
    """Copy one scan's served artifacts GCS → R2.

    The final "publish to the serving cloud" stage of an ingest that builds
    against GCS: snapshot JSONs + the layer-2 dir (`index/<gen>/` tiers,
    `.groups.json` manifests, age pyramids; on cw also the canonical parquets).
    Idempotent — same size + md5 already in R2 is skipped — so it doubles as
    the backfill over old scans. R2 via the env: R2_ENDPOINT, R2_BUCKET,
    R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (`s3` extra).

    Only the generations D1 points at are copied (the site reads no other),
    so a reindex's superseded `index/<gen>/` never reaches R2; that reads D1
    (`D1_DB_ID` + `CLOUDFLARE_API_TOKEN`/`CLOUDFLARE_ACCOUNT_ID`). `-a`
    copies every generation without it.
    """
    from . import publish as pub

    pointed = None
    if not all_gens:
        from .index_footer import pointers

        pointed = {d for _, _, d in pointers()}
    pub.publish(
        scan,
        src_bucket=src_bucket or pub.DATA_BUCKET,
        prefixes=list(prefixes) or None,
        subdir=pub.SNAPSHOTS_SUBDIR if subdir is None else subdir,
        layer2=layer2 or pub.LAYER2_PREFIX,
        dry_run=dry_run,
        workers=workers,
        listings=not no_listings,
        pointed=pointed,
    )
