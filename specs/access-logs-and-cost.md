# Two new planes: access-log ingest (read attribution) + cost import

Written 2026-08-14 from a marin-gcs-usage session (spec workflow). Context: the engine spec
(`gcs-backend-and-snapshot-diff.md`) is fully landed (items A–E). This spec adds the next two
capability planes, both generic-in-DT / overlays-in-consumer, mirroring the size-scan split.
Driving consumer: marin's GCP bill is **~half operations ("Class B" reads) + egress**,
which size scans can't see; GCS usage logging is now enabled on 6 buckets → hourly CSVs at
`gs://marin-usage-logs/usage/<bucket>/*` (delivery pending). Marin eng explicitly asked for an
ops auto-report. Urgency: plane 1 v1 is wanted within days of CSVs landing.

## Plane 1 — access logs (`dt access` namespace)

### Canonical access row (layer-1a)

One row per request, normalized across providers:

```
ts          TIMESTAMP   -- request time
store       VARCHAR     -- gcs | s3 | r2
bucket      VARCHAR
path        VARCHAR     -- object key ('' for bucket-level ops)
op          VARCHAR     -- normalized: GET|PUT|LIST|HEAD|DELETE|OTHER (keep raw in op_raw)
op_raw      VARCHAR     -- provider verb (cs_method+cs_operation for GCS)
status      SMALLINT
bytes_out   BIGINT      -- sc_bytes (egress-ish)
bytes_in    BIGINT      -- cs_bytes
requester   VARCHAR     -- best available identity: IP for GCS usage logs; canonical id for CloudTrail
user_agent  VARCHAR
```

### Sources / parsers

1. **GCS usage logs** (first; format is fixed & documented): hourly CSVs named
   `<prefix>_usage_<ts>_<id>_v0`, header row, fields incl. `time_micros, c_ip, cs_method,
   cs_uri, sc_status, cs_bytes, sc_bytes, cs_user_agent, cs_operation, cs_bucket, cs_object`.
   Parse with DuckDB `read_csv` over a glob (`gs://…/usage/<bucket>/*_usage_*`); dedupe on the
   `s_request_id` field (Google documents rare duplicate log lines). Also accept the sibling
   `_storage_` daily files (bucket byte-hours) as a trivial bonus table.
2. **S3 server access logs** (space-delimited text; well-known schema) and **CloudTrail data
   events** (JSON; has real `principalEmail`-equivalent identity) — parser stubs behind the
   same row shape; implement when an S3 consumer materializes.
3. **R2**: Logpush HTTP-request dataset when configured; document the "front with a Worker"
   caveat — no per-object server logs otherwise.

### Aggregation (layer-2a) — reuse the path-tree machinery

`dt access agg` (mirrors `import --engine duckdb`): out-of-core group-by from raw rows to
**per-path per-day per-op** rollups, then the same bottom-up parent synthesis used for size
scans, so results are a *path tree* (`path,parent,depth` + `n_ops,bytes_out,n_requesters` per
op-class per day). That makes the existing treemap/diff/series widgets work over *ops* and
*egress* the way they already work over bytes-at-rest: hot-prefix treemap = `<Treemap>` with a
`n_ops` accessor; ops-over-time = `<TimeSeries>`.

### Surfaces

- `dt access import <glob> [--store gcs] [--since/--until]` → canonical parquet
- `dt access agg` → layer-2a tree parquet (registered like scans, dated)
- `dt access top [-d depth] [-n N] [--op GET] [--by ops|bytes]` → hot-prefix table (the
  auto-report primitive; consumer formats it for Slack/Discord)
- server/UI: an ops-mode on the existing scan views (later; CLI + parquet first)

### Consumer-side (NOT in DT)

Requester→person joins (marin: IP/UA heuristics + path conventions + identities.yaml),
digest posting, cost-per-op pricing overlays.

## Plane 2 — cost import (`dt cost`, later)

Canonical cost rows (`period,account,project,service,sku,region,usage_amt,usage_unit,
list_cost,net_cost,credits`) from: GCP billing-console **CSV export** (available today —
no-API world), GCP **BigQuery billing export** (if enabled), AWS **CUR**, CF **GraphQL**.
Deliberately thin: parse + normalize + store; reconciliation/policy (gross-vs-net, edu
discounts, rebill markups) is consumer logic. Ship after plane 1; the marin need today is
served by manual CSV downloads.

## Compute placement (consumer note, learned 2026-08-14)

Bulk log processing must run **in the provider's cloud** (for marin: a GCE VM or the GCP
Batch job itself, us-central1). Volumes are ~10 GB/hr of CSV fleet-wide (~72 GB for the
first 7 delivered hours) — cross-cloud egress (e.g. to an AWS node) costs ~$0.12/GB and adds
WAN latency; the one-time AWS `mgu` smoke was fine but is not the pattern. This differs from
the *listing* plane, whose API-call traffic is negligible-egress and ran fine from AWS.

## Non-goals

- Real-time streaming (hourly/daily batch is the regime)
- Identity resolution inside DT (requester stays a raw string; joins are consumer overlays)
- R2 completeness parity (document the Worker-fronting option instead)

## Status

- [x] GCS usage-log parser (+ `s_request_id` dedupe, `_storage_` bonus table)
      — `disk_tree/access/parsers/gcs.py`; 3 fixture tests
- [x] `dt access import` / `agg` / `top` — `disk_tree/cli/access.py`
- [x] layer-2a parent-synthesis reuse — `disk_tree/access/aggregate.py`
      copies the `_PARENT_EXPR` shape from `find/aggregate_duckdb.py`
      (deliberate copy vs. import to keep the access module decoupled from
      the find module's SQL internals; if the parent-of policy ever needs
      to change, both files change together)
- [ ] widgets: ops accessors documented for `<Treemap>`/`<TimeSeries>` (likely zero code)
- [x] S3/CloudTrail parser stubs; R2 Logpush note — `parsers/{s3,r2}.py` raise
      `NotImplementedError` with pinned interfaces + provider-format doc
- [x] Real-data smoke against GCS-delivered CSVs — done 2026-08-14 on mgu:
      72GB of CSV (7h × 6 buckets) → 147.8M requests, 46.1TB `bytes_out`.
      Drove three fixes: `2de9578` (`preserve_insertion_order=false` — the
      parquet writer OOM'd on the 72GB import), `2b17684` (`DISTINCT ON` narrow
      projection instead of window-over-`SELECT *`, which materialized unused
      columns and OOM'd a 12GB cap on a 40M-request hour), `8849e23` (atomic
      write — a kill mid-`write_parquet` left a truncated shard that passed
      `[ -s ]` and broke downstream `agg`). Output at `tmp/access/{agg.parquet,
      top-ops.txt,top-bytes.txt}`; headline finding was that 69% of reads in the
      window hit `tmp/ttl=1d/zephyr/`.
- [x] layer-2a keys on `(bucket, path)` (2026-08-22) — `bucket` is a group key
      at every level; each bucket gets its own `.` root row. Same-named
      prefixes in different buckets no longer merge, and the webdata join is
      exact at per-bucket grain.
- [x] `max(ts)` per path — `last_ts` column (2026-08-22): MAX at the leaf
      grain, MAX-propagated up parent synthesis, so any prefix's atime covers
      everything under it (deeper than any depth cap included). `day`
      truncation pinned to UTC (was session-TZ-dependent). GCS-only: CAIOS
      returns `NotImplemented` for bucket logging.
- [x] `LIST` zero explained + fixed (2026-08-22): GCS spells listing
      `GET_Bucket` (XML API — an HTTP GET on the bucket) or
      `storage.objects.list` (JSON API); the case-sensitive `LIKE '%_BUCKET'`
      matched neither, so listings counted as GET (SQL path) / OTHER (python
      path). Both normalizers now match case-insensitively (+ JSON-API
      get/insert/patch/update/delete spellings).
- [x] scheduled/incremental ingestion (2026-08-22) — `gcs-usage access
      ingest|status` (marin/src/gcs_usage/access.py): per-bucket name
      watermark + 6h lag-window re-list with an ingested-name tail (late
      deliveries get picked up, not skipped); chunked (≤64GB CSV) stage →
      parse → lossless layer-1a (zstd) → layer-2a agg shard → upload →
      advance watermark. Crash reprocesses ≤1 chunk onto the same object
      names (idempotent). Runs in-GCP on every scheduled Batch attempt
      (job/run.sh, before the NOP gate; `ACCESS_ONLY=1` = ingest-only run).
      NB delivery layout is FLAT — `usage/<bucket>_usage_<ts>_<id>_v0`
      (bucket = filename prefix), not the `usage/<bucket>/*` this spec
      originally assumed.
      Backfill completed 2026-08-22 ~22:14 UTC (2.86 TB → 336 GB layer-1a,
      579 MiB layer-2a, all 7 buckets). Gotcha found 2026-08-23: the daily
      scheduler's static Batch template predated `DUCKDB_MEM_ACCESS`, so the
      first scheduled ingest ran at the laptop-default 8GB cap and OOM'd its
      parquet write (soft-fail; snapshot still published with `-x`). Fixed
      both ends: scheduler body now carries `DUCKDB_MEM_ACCESS=24GB`, and
      run.sh defaults it to 24GB so template drift can't regress it.
- [ ] `dt cost` plane (deferred)

Post-landing (2026-08-14): all core scaffolding + GCS parser + fixture tests
in `6181b1c`+. Once marin's usage-log CSVs land, `disk-tree access import
gs://marin-usage-logs/usage/<bucket>/* -o /tmp/canonical.parquet` should
Just Work; anything that doesn't is a real-data-driven follow-up.

## Productionize ingest (2026-08-19)

Measured growth after 6 days of delivery (`gcloud storage du`, all objects in
`gs://marin-usage-logs/usage/`): **1.50 TiB / 13,510 CSVs**, ~300 GiB/day
steady state — ~9 TiB/month if left raw, with **no lifecycle policy** on the
bucket. Per source bucket (6d totals): us-central1 618 GiB, us-central2
435 GiB, us-east5 375 GiB, eu-west4 103 GiB, us-west4 7 GiB, us-east1 2 GiB,
us-west1 ~0.

Plan (cron over event-driven) — items 1–3 landed 2026-08-22 (see Status
checklist; layout note: layer-1a shards are per-chunk `part-<first>--<last>`
under `access/raw/<bucket>/`, not `bucket/day/` partitions — the `day` column
inside serves the same pruning):

1. **Incremental ingest job on GCP Batch**, same pattern as the scan crons
   (NVMe staging, runs where the data lives — see Compute placement above).
   Watermark = last ingested object name (delivery names embed the log hour
   and sort lexicographically per bucket); each run lists names past the
   watermark, so re-runs are idempotent and a missed run self-heals.
   Cadence /6h piggybacking the existing schedule is plenty — the dashboard
   refreshes /6h anyway, so event-driven (OBJECT_FINALIZE → Pub/Sub → Cloud
   Run) buys ~nothing in freshness and adds a second infra shape; revisit
   only if we ever want near-real-time read attribution.
2. **Lossless layer-1a parquet** (zstd, partitioned `bucket/day/`): every CSV
   row preserved. Usage CSVs are wide and repetitive; expect ≥10× compression
   (~30 GiB/day → ~1 TiB/yr, vs ~110 TiB/yr raw).
3. **Lossy layer-2a agg** per run (existing `dt access agg` shape +
   the `(bucket, path)` key fix and `max(ts)` atime column from the Status
   checklist) → feeds the dashboard age/atime lens.
4. **Lifecycle on the raw CSVs** — landed 2026-08-23 (user go-ahead), and
   NB the bucket turned out to already have a bucket-wide Delete@30d rule
   (this spec's "no lifecycle" note was stale). Final shape: after each
   ingest, `sweep_ingested` moves converted CSVs from `usage/` to
   `ingested/`, where a `matchesPrefix` rule deletes at age 7d (the copy
   resets the age clock → 7d *post-conversion*); never-ingested files keep
   the 30d bucket-wide Delete as the backstop, so a broken ingest has a
   month to be noticed before raw loss. Job SA got `objectAdmin` on
   marin-usage-logs for the moves. Layer-1a retention (also 2026-08-23):
   `access/raw/` on oa-gcs-usage-dvx transitions to Coldline @30d and
   deletes @180d (~34 GB/day → ~6 TB ring, ~$40/mo; ≥150d in Coldline
   clears the 90d minimum). 1a is archival/reprocessing only — the UI
   serves tree.json built from layer-2a, so Coldline retrieval fees never
   hit the serving path. Layer-2a (~25 MiB/day) is kept forever.

## Webdata / UI (landed 2026-08-22)

`gcs-usage webdata -x <agg glob>` joins per-(bucket, path) `MAX(last_ts)`
over read ops (GET/HEAD/LIST, depth ≤ 4) into the snapshot: tree nodes gain
`a` (last-read epoch day; MAX over the subtree), meta gains
`access: {from, to}` (observation window). Site: `read` color mode (viridis
over the window; never-read = `--never-read` brick), "last read" tooltip
line, sortable read columns in the /mark worklists + children table, and
Lost & found orders coldest-first (never-read → least-recently-read).

