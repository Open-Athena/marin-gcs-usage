# Productionize the W&B attribution mine

## Why

`attr/attribution-wandb.parquet` is a **static artifact, last mined 2026-07-23** — while runs keep landing daily. Found 2026-08-26: 840 of 1,060 `marin-us-central2/grug/swarm_*` dirs (~160 TB, Calvin's) showed *unattributed*; the 220 dirs the parquet covers attribute fine. Same staleness affects `marin-us-east5/checkpoints/exp_sft_*selfinstill*` and anything else newer than 7/23. Percy flagged the resulting ~500 TB gray block on the 8/18 call. Attribution should be as fresh as the snapshot it colors.

**Two failure modes, not one.** The series index shows `grug` grew only +24 TB over 7/30→8/26 — so most of the ~157 TB of unattributed swarm bytes *predate* the 7/23 mine and were **missed by it**, not merely newer than it. The likely culprit: the pre-bisection miner swept dense projects with one paginated query, which silently truncates (same class of bug as the project-listing truncation the miner now retries). So freshness alone doesn't fix attribution — completeness of the mine matters equally, and only the windowed (bisecting) miner is trustworthy on dense projects.

## Shape: a step in the daily snapshot job

Fold into `job/run.sh` between the listing fan-out and `webdata` (the node already has everything needed — listings staged on NVMe, 128G RAM):

1. **Mine (incremental):** `gcs-usage wandb-mine -o $STAGE/wandb-runs.parquet -s <last-asof - 7d>` — a rolling window (re-mine the trailing week to catch late-arriving runs), **merged with the prior runs parquet** kept in the bucket (`attr/wandb-runs.parquet` becomes a persisted, append-merged artifact, like the access-log watermark model). Full re-mine (~211 projects, tens of minutes) stays a manual/backfill path.
2. **Attr:** `gcs-usage wandb-attr -r $STAGE/wandb-runs.parquet -l <staged listings> [-x executor-infos] -o /gcs/$DATA/attr/attribution-wandb.parquet` — regenerates the whole attribution parquet from the merged runs against *today's* listing (cheap relative to webdata: one DISTINCT-dirs pass + joins).
3. `webdata` consumes it same-run (AG already points at `attr/attribution-wandb.parquet` — no further change).

Guard like the access-ingest step: failure warns and continues with the prior parquet (snapshot must not block on W&B API health); next run self-heals.

## Requirements

- `WANDB_API_KEY` → Secret Manager (`gcs-usage-wandb-api-key`), wired as a `secretVariables` entry in `job/batch-submit.sh` alongside the Slack tokens.
- `wandb` pip dep baked into the Batch image (`marin[wandb]` extra in the Dockerfile install).
- `wandb-mine` needs a `--merge-with <prior.parquet>` (or the run.sh step DIY-merges: read prior + new, dedup on run id, write).
- `executor-mine` (sidecar signal) has the same staleness problem — same treatment, same step.

## One-time backfill (in progress, 2026-08-26)

Run manually on `mgu`: full `wandb-mine` (211 projects) + `wandb-attr` against the 8/26 listing (synced to the node via the read-only HMAC S3-compat creds), upload to `attr/attribution-wandb.parquet`. Tomorrow's snapshot then attributes the swarm/selfinstill backlog; optionally `REPROC=1` today's date to see it sooner.

## Performance & resumability (as built, 2026-08-26)

- The miner was **already interruptible/resumable**: every leaf window flushes its own part parquet under `<out>-parts/` (skip-if-exists); a rerun fills gaps and merges. An interrupt loses at most the in-flight windows (≤2,000 runs each).
- Added **`-j/--jobs`**: a work-queue of `(project, window)` tasks over a thread pool — an oversized window enqueues its halves (`spawn`) instead of recursing, so one dense project fans out across all workers and no task blocks on another. Network-bound, so threads suffice.
- **Rate limits are per API key**; the serial miner is latency-bound (no 429s observed), so same-key `-j 8-12` should scale near-linearly. Multi-machine sharding via `--since/--until` bisection edges (`-E`) remains available on top.

## Cadence (revised by the missed-runs finding)

- Daily incremental: trailing-week `createdAt` window (catches new runs; old runs' new output dirs attribute via the merged runs-parquet × fresh listing join).
- **Weekly full re-mine as a required backstop** — not optional: it's what catches runs any earlier sweep dropped (the grug/swarm failure) and post-creation config edits. With `-j` and warm parts (only gap windows re-fetch... note: full re-mine wants a fresh parts dir or a `--refetch` flag, else skip-if-exists returns stale parts — open question below).

## Open questions

- Persist raw runs parquet in the bucket (proposed) vs re-mine full history every run (simpler, slower, more W&B API load).
- Full-re-mine vs parts caching: part files make reruns cheap but *pin* window contents; a weekly backstop should invalidate parts whose window overlaps recent time (or start clean). Simplest: parts dir keyed by mine-date for full sweeps.
- The `asof` column: rows carry mine-date; keep newest row per prefix on merge so re-mines refresh evidence.
