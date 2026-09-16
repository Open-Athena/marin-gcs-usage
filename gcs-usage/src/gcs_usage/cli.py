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
import re
import sys
from collections import Counter
from dataclasses import asdict
from functools import partial
from pathlib import Path

import duckdb
import pandas as pd
from click import Choice, argument, group, option

from .digest import REPLY_HOUR_UTC
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


# --- index tiers + their D1 footers (specs/view-serving.md, ported from gcs) ---


@main.command("index-write")
@option("-b", "--bucket", default=None, help="Bucket the layer-2 parquet describes (default $CW_BUCKET); prefixes every index path")
@option("-m", "--mem", default="8GB", help="DuckDB memory limit")
@option("-o", "--out", "out_dir", type=Path, required=True, help="Output dir: path-index.parquet + path-index-coarse<E>.parquet")
@option("-t", "--threads", default=8, type=int, help="DuckDB threads")
@option("-T", "--tmp", "tmp_dir", type=Path, default=None, help="DuckDB spill dir (default: <out>/.duckdb-tmp)")
@argument("l2_parquet")
def index_write(bucket: str | None, mem: str, out_dir: Path, threads: int, tmp_dir: Path | None, l2_parquet: str) -> None:
    """Write the scan's index tiers from its layer-2 parquet: the floor-free
    `path-index.parquet` (dir rows, bucket-prefixed, sorted (depth, path), 8k-row
    groups, the site's column contract) and the coarse tiers, floors in their
    parquet metadata. `index-sync` then publishes their footers to D1."""
    from .index import write_index
    from .sweep import CW_BUCKET

    s = write_index(l2_parquet, out_dir, bucket=bucket or CW_BUCKET, mem=mem, threads=threads, tmp_dir=tmp_dir)
    err(f"index-write: {s['rows']:,} rows; floors {s['floors']}; kept {s['paths']}")
    print(json.dumps(s))


@main.command("index-sync")
@option("-b", "--bucket", default="oa-gcs-usage-dvx", help="Data bucket holding the index tiers")
@option("-C", "--coarse-only", is_flag=True, help="Only the coarse tiers")
@option("-d", "--dir", "listing_dir", default=None, help="Local/mounted dir holding the parquets (default: <bucket>/<key>)")
@option("-F", "--floor-free-only", is_flag=True, help="Only the floor-free variant")
@option("-g", "--gen", required=True, help="Generation stamp these files belong to (the run's GEN)")
@option("-k", "--key", default=None, help="Bucket-relative dir the parquets live under — what the site reads (default: cw-l2/<scan>/index/<gen>)")
@option("-L", "--local", is_flag=True, help="Write to the local wrangler D1 instead of --remote")
@option("-v", "--variant", "variants", multiple=True, help="Only sync these variants (default: all)")
@argument("scan")
def index_sync(bucket: str, coarse_only: bool, listing_dir: str | None, floor_free_only: bool, gen: str, key: str | None, local: bool, variants: tuple[str, ...], scan: str) -> None:
    """Publish a scan's index-tier footers to D1 (index_row_groups + the
    index_schema pointer) — one generation of files under one bucket dir. Per
    variant the row groups land first, tagged with the generation, and the
    pointer (gen, dir) flips last, so the site moves from the previous complete
    generation to this one with no window. Needs CLOUDFLARE_API_TOKEN +
    CLOUDFLARE_ACCOUNT_ID in the env."""
    from .index import INDEX_VARIANTS
    from .index_footer import sync_d1

    key = key or f"cw-l2/{scan}/index/{gen}"
    base = listing_dir or f"{bucket}/{key}"
    todo = variants or tuple(INDEX_VARIANTS)
    if coarse_only:
        todo = tuple(v for v in todo if v.startswith("coarse"))
    if floor_free_only:
        todo = tuple(v for v in todo if not v.startswith("coarse"))
    for variant in todo:
        if variant not in INDEX_VARIANTS:
            raise SystemExit(f"index-sync: unknown variant {variant!r} (want one of {list(INDEX_VARIANTS)})")
        n = sync_d1(scan, f"{base}/{INDEX_VARIANTS[variant]}", variant=variant, gen=gen, key=key, remote=not local)
        err(f"index-sync: {scan} [{variant}] gen {gen} @ {key} — {n} row groups ({'local' if local else 'remote'})")


@main.command("index-gc")
@option("-r", "--retain", type=int, default=None, help="Also retire the floor-free tier's row groups for every synced scan older than the newest N (the coarse tiers stay)")
@argument("scans", nargs=-1)
def index_gc(retain: int | None, scans: tuple[str, ...]) -> None:
    """Drop the row groups of generations no pointer names (leftovers of a
    flip or a failed sync) for SCANS; with -r, retention on top."""
    from .index_footer import gc_d1, retire_d1

    for s in scans:
        err(f"index-gc: {s}: {gc_d1(s)} orphan row groups deleted")
    if retain is not None:
        for d, v, n in retire_d1(retain):
            err(f"index-gc: retired {d} [{v}]: {n} row groups")


@main.command("index-dir")
@option("-v", "--variant", default="path", help="Index variant (default path)")
@argument("scan")
def index_dir_cmd(variant: str, scan: str) -> None:
    """Print the bucket-relative dir D1 points at for SCAN's VARIANT (exit 1 if unsynced)."""
    from .index_footer import index_dir

    d = index_dir(scan, variant)
    if not d:
        raise SystemExit(f"index-dir: {scan} [{variant}] not synced")
    print(d)


@main.command("warm-cache")
@option("-d", "--date", "scan", default=None, help="Scan to warm (default: newest under --root)")
@option("-j", "--jobs", default=4, help="Concurrent requests")
@option("-n", "--dry-run", is_flag=True, help="Print the request plan, fetch nothing")
@option("-r", "--root", default=None, help="Snapshots root (default gs://$DATA_BUCKET/snapshots/cw)")
@option("-u", "--url", "site_url", default=None, help="Site base URL (default https://cw-s3.oa.dev)")
@option("-w", "--widths", default=None, help="Comma-separated canvas widths (default: the common laptop/phone set)")
def warm_cache(scan: str | None, jobs: int, dry_run: bool, root: str | None, site_url: str | None, widths: str | None) -> None:
    """Replay the home page's default views for a fresh scan so its first
    viewer hits the edge cache. Auth: a Cloudflare Access *service token* —
    CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET in the env (the site is
    whole-host Access-gated; there is no agent bearer token here)."""
    import fsspec

    from . import warm as W
    from .digest import DEFAULT_URL

    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots/cw"
    fs, _, _ = fsspec.get_fs_token_paths(root)
    scans = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in fs.ls(root.split("://", 1)[-1], detail=False) if re.search(r"/\d{4}-\d{2}-\d{2}(T\d{4})?/?$", p + "/"))
    scan = scan or (scans[-1] if scans else None)
    if not scan:
        raise SystemExit("warm-cache: no scans")
    ws = tuple(int(x) for x in widths.split(",")) if widths else W.WIDTHS
    plan = W.plan(scan, scans, widths=ws)
    if dry_run:
        print("\n".join(plan))
        return
    cid, csec = os.environ.get("CF_ACCESS_CLIENT_ID"), os.environ.get("CF_ACCESS_CLIENT_SECRET")
    if not (cid and csec):
        raise SystemExit("warm-cache: need CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET (an Access service token)")
    res = W.warm(site_url or DEFAULT_URL, {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": csec}, plan, jobs=jobs)
    bad = [r for r in res if r[1] != 200]
    err(f"warm-cache: {len(res) - len(bad)}/{len(res)} ok" + (f"; {len(bad)} failed" if bad else ""))


@main.group()
def lifecycle() -> None:
    """Bucket lifecycle rules as a tracked file: `pull` (live → JSON), `diff`
    (file vs live), `push` (file → bucket, whole-config PUT + read-back
    verification), `gc-rule` (print the bucket-wide noncurrent-version GC rule
    to add to the file). Creds: the CAIOS keys from the env (see `sweep`)."""


@lifecycle.command("pull")
@option("-b", "--bucket", default=lambda: os.environ.get("CW_BUCKET", "marin-us-east-02a"), help="Bucket (default $CW_BUCKET)")
@option("-o", "--out", type=Path, help="Write here instead of stdout")
def lifecycle_pull(bucket: str, out: Path | None) -> None:
    from .lifecycle import dump, pull
    from .sweep import s3_client

    text = dump(pull(s3_client(), bucket))
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text)
        err(f"lifecycle: {bucket} → {out}")


@lifecycle.command("diff")
@option("-b", "--bucket", default=lambda: os.environ.get("CW_BUCKET", "marin-us-east-02a"), help="Bucket (default $CW_BUCKET)")
@argument("path", type=Path)
def lifecycle_diff(bucket: str, path: Path) -> None:
    """Exit 1 when PATH (intended) differs from the live rules."""
    from .lifecycle import diff, load, pull
    from .sweep import s3_client

    d = diff(load(str(path)), pull(s3_client(), bucket))
    print(json.dumps(d))
    if any(d.values()):
        sys.exit(1)


@lifecycle.command("push")
@option("-b", "--bucket", default=lambda: os.environ.get("CW_BUCKET", "marin-us-east-02a"), help="Bucket (default $CW_BUCKET)")
@option("-n", "--dry-run", is_flag=True, help="Print the diff that would be applied; touch nothing")
@argument("path", type=Path)
def lifecycle_push(bucket: str, dry_run: bool, path: Path) -> None:
    """Replace the bucket's lifecycle configuration with PATH (read back + verified)."""
    from .lifecycle import diff, load, pull, push
    from .sweep import s3_client

    client = s3_client()
    intended = load(str(path))
    d = diff(intended, pull(client, bucket))
    if not any(d.values()):
        err(f"lifecycle: {bucket} already matches {path}")
        return
    err(f"lifecycle: {'would apply' if dry_run else 'applying'} to {bucket}: {json.dumps(d)}")
    if dry_run:
        return
    live = push(client, bucket, intended)
    err(f"lifecycle: {bucket} now has {len(live)} rule(s), verified")


@lifecycle.command("gc-rule")
@option("-d", "--days", default=1, help="NoncurrentDays (1 while versioning is off; the undo window when it's on)")
@option("-p", "--prefix", default="", help="Scope (default: whole bucket)")
def lifecycle_gc_rule(days: int, prefix: str) -> None:
    from .lifecycle import gc_rule

    print(json.dumps(gc_rule(days, prefix), indent=2))


@main.group()
def sweep() -> None:
    """Mark & sweep: build deletion manifests and execute them (boto3/CAIOS)."""


@sweep.command("manifest")
@option("-d", "--date", required=True, help="Scan id (SNAP_ID) whose layer-2 parquet to pin")
@option("-l", "--l2", "l2_path", help="Layer-2 parquet path (default: /gcs/<data>/cw-l2/<date>/<bucket>.parquet)")
@option("-o", "--out", required=True, help="Output dir for manifest/ + plan-summary.json")
@argument("plan_path")
def sweep_manifest(date: str, l2_path: str | None, out: str, plan_path: str) -> None:
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


@sweep.command("expire-manifest")
@option("-b", "--bucket", default=None, help="Bucket (default $CW_BUCKET)")
@option("-e", "--early-days", type=float, default=0.0, help="Also take objects within this many days of their TTL (age >= N - EARLY_DAYS)")
@option("-o", "--out", required=True, help="Output run dir for manifest/ + plan-summary.json (what `sweep execute` consumes)")
@option("-t", "--now-ts", type=int, default=None, help="Epoch seconds to age against (default: now)")
@argument("l2_parquet")
def sweep_expire_manifest(bucket: str | None, early_days: float, out: str, now_ts: int | None, l2_parquet: str) -> None:
    """Manifest of the `tmp/ttl=<N>d/` objects past (or within EARLY_DAYS of)
    their TTL, from the layer-2 parquet L2_PARQUET, in the run-dir layout
    `sweep execute` consumes. Objects younger than their TTL under the same
    roots are not in the manifest, so the executor counts them as drift and
    leaves them alone."""
    import json
    import time

    from .sweep import CW_BUCKET, build_expiry_manifest

    s = build_expiry_manifest(l2_parquet, out, bucket=bucket or CW_BUCKET, now_ts=now_ts or int(time.time()), early_days=early_days)
    err(f"expire-manifest: {s['objects']} objects / {s['bytes']} bytes across {s['sweep']} -> {s['manifest']}")
    print(json.dumps(s))


@sweep.command("execute")
@option("-G", "--no-versioning-guard", is_flag=True, help="Skip the versioning preflight: a real delete is then PERMANENT (no delete marker to undo)")
@option("-r", "--for-real", is_flag=True, help="Actually delete (writes recoverable delete markers); default is a dry run")
@argument("run_dir")
def sweep_execute(no_versioning_guard: bool, for_real: bool, run_dir: str) -> None:
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


@sweep.command("undo")
@option("-n", "--dry-run", is_flag=True, help="Report what would be restored without touching anything")
@option("-p", "--prefix", "prefixes", multiple=True, help="Restrict undo to keys under this prefix (repeatable)")
@argument("run_dir")
def sweep_undo(dry_run: bool, prefixes: tuple[str, ...], run_dir: str) -> None:
    """Undo a real run under RUN_DIR: remove its delete markers (recoverable
    delete). Must run before `purge`."""
    import json

    from .sweep import undo_run

    s = undo_run(run_dir, prefixes=list(prefixes) or None, dry_run=dry_run)
    err(f"{'DRY ' if dry_run else ''}undo: restored {s['restored']} (failed {s['restore_failed']}, skipped {s['skipped']})")
    print(json.dumps(s))


@sweep.command("purge")
@option("-n", "--dry-run", is_flag=True, help="Report what would be purged without touching anything")
@argument("run_dir")
def sweep_purge(dry_run: bool, run_dir: str) -> None:
    """Permanently drop every version of a real run's deleted keys under RUN_DIR
    — the irreversible space-reclaim stage, after the undo hold."""
    import json

    from .sweep import purge_run

    s = purge_run(run_dir, dry_run=dry_run)
    err(f"{'DRY ' if dry_run else ''}purge: {s['purged_versions']} versions / {s['purged_bytes']} bytes (failed {s['purge_failed']})")
    print(json.dumps(s))


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


def _icons_dir() -> Path:
    """`job/icons-cw` in both layouts: pip-installed in the job image (cwd=/app →
    /app/job/icons-cw) or the repo checkout (…/parents[3]/job/icons-cw)."""
    cands = (Path.cwd() / "job" / "icons-cw", Path(__file__).resolve().parents[3] / "job" / "icons-cw")
    return next((c for c in cands if c.exists()), cands[-1])


@main.command()
@option("-c", "--channel", help="Slack channel id (default $SLACK_CHANNEL)")
@option("-D", "--reply-delay", "reply_delay", default=0.0, type=float, help="Seconds to sleep between replies (e.g. 305 for a spaced backfill so per-reply sender chrome survives)")
@option("-F", "--for-real", is_flag=True, help="With --redo-replies: actually post the new replies and delete the old ones (default: print the plan)")
@option("-H", "--reply-hour", type=int, default=REPLY_HOUR_UTC, help="UTC hour the sender variant's daily reply is taken from: the day's first scan at/after it (default 12 → the 12:01Z morning scan, 8:01 am ET; 00:01Z scans still feed the OP + plot)")
@option("-i", "--icons-dir", type=Path, default=None, help="Where the plot PNG is written + deployed from (default job/icons-cw)")
@option("-m", "--month", help="Month YYYY-MM (default: current UTC month)")
@option("-n", "--dry-run", is_flag=True, help="Render the plot + print OP/replies; post & host nothing")
@option("-r", "--root", help="Snapshots root (default gs://$DATA_BUCKET/snapshots/cw)")
@option("-t", "--token", help="Slack bot token (default $SLACK_BOT_TOKEN)")
@option("-u", "--url", "site_url", default=None, help="Site base for links (default cw-s3.oa.dev)")
@option("-R", "--redo-replies", is_flag=True, help="Re-post the month's replies under the current day rule, then delete the old ones (dry-run unless --for-real)")
@option("-V", "--variant", type=Choice(["sender", "body"]), default="sender", help="Reply style: headline as the sender name, posted once from the day's morning scan (sender) or bold in the body, edited as the day's scans land (body)")
def digest(channel: str | None, reply_delay: float, for_real: bool, reply_hour: int, icons_dir: Path | None, month: str | None, dry_run: bool, redo_replies: bool, root: str | None, token: str | None, site_url: str | None, variant: str) -> None:
    """Converge the monthly digest thread in #cw-s3-usage: an OP edited in place
    (month-to-date + weekly bullets + quota sparkline) + one reply per UTC day,
    via thrds. State in gs://<bucket>/digest/cw/<channel>/<variant>/<YYYY-MM>.json.
    See specs/cw-slack-digest.md."""
    from . import digest as dg

    site_url = site_url or dg.DEFAULT_URL
    m = (
        dt.datetime.strptime(month, "%Y-%m").date()
        if month
        else dt.datetime.now(dt.timezone.utc).date().replace(day=1)
    )
    root = root or f"gs://{os.environ.get('DATA_BUCKET', 'oa-gcs-usage-dvx')}/snapshots/cw"

    if dry_run:
        month = dg.load_month(root, m)
        if month is None:
            raise SystemExit(f"digest: no scans for {m:%Y-%m}")
        import tempfile

        out = Path(tempfile.gettempdir()) / f"cw-digest-{m:%Y%m}.png"
        dg.render_plot(month, m, out, root)
        err(f"rendered plot → {out}")
        print(dg.op_body(month, m, "<plot-url>", site_url))
        print(f"\n--- replies ({variant}: username | body | icon) ---")
        for day in dg.day_rows(month, variant, reply_hour):
            r = dg.reply(day, variant, site_url)
            print(f"{r.username} | {r.body} | {(r.icon_url or r.icon_emoji or '').split('/')[-1]}")
        return

    channel = channel or os.environ.get("SLACK_CHANNEL")
    token = token or os.environ.get("SLACK_BOT_TOKEN")
    if not (channel and token):
        raise SystemExit("digest: need SLACK_BOT_TOKEN + SLACK_CHANNEL (or -t/-c)")
    icons = icons_dir or _icons_dir()

    def deploy(local: Path, name: str) -> str | None:
        # publish the cw icons dir (the CORS _headers + the fresh plot) to the
        # icons Pages project's `cw` preview branch — never its production
        # branch, whose root alias serves the arrow avatars both digests use.
        # Return the deployment-specific URL (served instantly), which the OP
        # image uses to avoid racing alias propagation (→ Slack invalid_blocks).
        import re
        import shutil
        import subprocess

        # The job image installs wrangler globally (`npm install -g`) but has
        # no `npx` shim, so prefer the binary; `npx` only serves a laptop run.
        wrangler = [shutil.which("wrangler")] if shutil.which("wrangler") else ["npx", "wrangler"] if shutil.which("npx") else None
        if wrangler is None:
            raise SystemExit("digest: neither `wrangler` nor `npx` on PATH — can't publish the plot")
        r = subprocess.run(
            [*wrangler, "pages", "deploy", str(icons), "--project-name", dg.ICONS_PROJECT, "--branch", dg.ICONS_BRANCH, "--commit-dirty=true"],
            check=True, capture_output=True, text=True,
        )
        err(r.stdout)
        found = re.search(r"https://[a-z0-9]+\.gcs-usage-icons\.pages\.dev", r.stdout + r.stderr)
        return found.group(0) if found else None

    if redo_replies:
        # rule change: re-post every reply under the current day rule, then retire the old ones
        plan = dg.redo_replies(root, m, token, channel, variant, site_url=site_url, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay, reply_hour=reply_hour, for_real=for_real)
        if for_real:
            err(f"digest: re-threaded {m:%Y-%m} ({variant}): {len(plan.get('posted', {}))} replies" + (f", {len(plan['stale'])} old left undeleted" if plan.get("stale") else ""))
            return
        old = {day: e for day, e in plan["old"]}
        print(f"digest --redo-replies {m:%Y-%m} in {channel} ({variant}; dry-run — -F/--for-real applies):")
        print(f"  old replies to delete: {len(plan['old'])}")
        for day, e in plan["old"]:
            print(f"    {day}  {e['scan']}  ts={e['ts']}")
        print(f"  new replies to post: {len(plan['new'])}")
        for day, scan, head in plan["new"]:
            same = "  (same scan as the old reply)" if day in old and old[day]["scan"] == scan else ""
            print(f"    {day}  {scan}  {head!r}{same}")
        return
    dg.post_digest(root, m, token, channel, variant, site_url=site_url, icons_dir=icons, deploy_plot=deploy, reply_delay=reply_delay, reply_hour=reply_hour)
    err(f"digest: converged {m:%Y-%m} ({variant})")



if __name__ == "__main__":
    main()
