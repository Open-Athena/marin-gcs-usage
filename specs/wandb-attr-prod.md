# Productionize the W&B attribution mine

## Why

`attr/attribution-wandb.parquet` is a **static artifact, last mined 2026-07-23** — while runs keep landing daily. Found 2026-08-26: 840 of 1,060 `marin-us-central2/grug/swarm_*` dirs (~160 TB, Calvin's) showed *unattributed* purely because they post-date the mine; the 220 dirs the parquet covers attribute fine. Same staleness affects `marin-us-east5/checkpoints/exp_sft_*selfinstill*` and anything else newer than 7/23. Percy flagged the resulting ~500 TB gray block on the 8/18 call. Attribution should be as fresh as the snapshot it colors.

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

## Open questions

- Persist raw runs parquet in the bucket (proposed) vs re-mine full history every run (simpler, slower, more W&B API load).
- Mine cadence: every daily run vs weekly + on-demand (attribution drift is slow; daily is cheap once incremental).
- The `asof` column: rows carry mine-date; keep newest row per prefix on merge so re-mines refresh evidence.
