# Scan runs in the site UI

Show each scan job's run the way `/staged` shows deletion runs: when it ran, how long each phase took, what it wrote (with links to the `/meta` tree and the map at that scan), what changed in each structure against the previous scan, and the job's Batch and log links.

## Status

- Built on `scan-runs` (off `cloud`): migration, `dt-cloud scan-run record | backfill`, `/api/scan-runs`, `/scans` and `/scans/<run>`, tests.
- **Gated on Ryan's go**: applying the migration to a deployment's D1 (gcs's prod D1 `oa-gcs-usage-auth` is also dev.gcs.oa.dev's) and the backfill's write to it. Until then the backfill has run read-only into a local SQLite file, and the UI was checked against a local D1 seeded from it.
- The job glue (§Job glue) is for the deployment sessions to apply on `gcs` / `cw-s3`.

## Decisions

### D1, not a `run.json` in the bucket

The record lives in D1 beside `deletion_runs`, not as a per-scan manifest object:

- **Listing is the main read.** `/scans` lists every run with its totals and deltas; from D1 that is three indexed `SELECT`s, from the bucket it is a LIST plus one GET per run (hundreds, growing) or a maintained index file with read-modify-write races between the job and the backfill.
- **It matches deletion runs**: the same store, the same `requireViewer` read path, the same "the job writes its own record" model (`sweep_exec.record_run`).
- **Additive only**: three new tables, no existing table altered or rebuilt, no FK into an existing table (memory: D1 enforces FKs; gcs 0028). The job already holds D1 write credentials for `index-sync`.
- No `run.json` mirror: it is not free (a second writer path, a second shape to keep in step), and nothing reads it.

### The schema (`site/migrations/cw/0016_scan_runs.sql`; gcs: the same file as `0040_scan_runs.sql`)

- `scan_runs`: one row per **job run** (not per scan: a scan can have a failed attempt, the run that published it, REPROCs, and its listing fan-out jobs). Key: the Batch job uid (`$BATCH_JOB_UID`, also the task logs' `labels.job_uid`). Columns: `store`, `scan`, `kind` (`scan | reproc | listing | …`, the profile's), `parent` (a downstream job's orchestrating run; no FK, either may land first), `status` (`running | succeeded | failed | nop`), `failed_phase`, `error`, `started_ts` / `finished_ts` (the task's RUNNING → terminal events), `job_name`, `region`, `machine`, `spot`, `tasks`, `image`, `cost_usd`, `gen` (the index generation it wrote), `source` (`job | backfill`; the first writer's), `updated_ts`.
- `scan_run_phases (run_id, phase)`: `seq`, `started_ts`, `finished_ts`, `status`, `note`. A phase starts where the previous one ended; an *overlapped* phase (gcs's `access-ingest`, which runs beside the listing) starts at the run's start.
- `scan_run_outputs (run_id, key)`: `uri`, `bytes`, `objects`, `rows`, `gen`. Keys are structures: `listing` and `listing/<bucket>` (rows = the bucket's `_SUCCESS.json` objects), `dir-cache`, `path-index` and `path-index/<tier>` (rows = the tier's last `row_end` in its `.groups.parquet`), `snapshot`, `static` and `static/deltas/<scan>` (the static name index's manifest: its appended runs' rows and bytes). The interval store gets an entry when its runs land (`specs/interval-store.md`).
- **Deltas are not stored**: the reader diffs each output against the previous scan's (the newest earlier scan with that structure; a key's scan id is normalized, so `static/deltas/<scan>` compares run to run). Stored deltas would go stale on every REPROC or late backfill.
- Writers upsert the parent row first and never `INSERT OR REPLACE` it (a REPLACE deletes the row, which the children's FKs refuse); a NULL in a later partial write keeps the stored value.

### `dt-cloud scan-run` (generic; deployment config in a profile)

A deployment's facts live in its profile JSON (`-c` / `$SCAN_RUNS_PROFILE`, checked in on the deployment branch as `job/scan-runs.json`): its GCP project and Batch regions; its **job kinds** (regex matches on job name, image, entrypoint, commands, env present / absent; where each finds its scan id: an env var, a commands regex, a log marker, the task's start or the job's creation formatted as a scan id; `downstream` for fan-out jobs); its phase / NOP / error markers; its **outputs** (URI templates over `{scan}` / `{gen}`, the phase whose end measures each, the kinds that write it, `split: dir | stem`, how rows are read); machine **prices** (list $/h) and a SPOT factor. `cloud` carries no deployment's profile.

- `record SCAN` — the job's incremental write, idempotent, and never fatal (an error prints and exits 0): `-S` start, `-P NAME` a phase just ended (measures the outputs tied to it), `-x RC` finish (0 = succeeded, else failed with `-F` phase and `-e` reason), `-N` a NOP, `-g GEN`, `-J JOB -R REGION` fill the Batch facts and cost, `-o KEY=URI` ad-hoc outputs. Sinks: D1 over the HTTP API (`$D1_DB_ID` + the `index-sync` credentials), `-s FILE` a local SQLite file (tables from the migration file), `-n` the SQL on stdout (`wrangler d1 execute --file` takes it).
- `backfill` — reconstruct past runs (§Backfill), same sinks; `-w DIR` saves the fetched jobs and log lines, `-j` / `-l` replay them; `-M` skips the store reads; `-S` from a scan id on.

Scan ids go through `dt_cloud.scan_id` (no private regexes, no "daily"): a job's scan source must yield `is_scan_id`.

### The UI

- `/scans`: one row per orchestrating run (downstream jobs fold into their parent), newest first: scan (→ map at `?d=<slug>`), kind, status (failed rows say where/why), started, a duration bar split by phase, output bytes and Δ vs the previous scan, cost, links. Above it, a DIY-SVG strip of duration per scan (stacked by phase) with failures marked.
- `/scans/<run id>`: the phase timeline (a Gantt on the run's clock, overlapped phases on their own lane), the outputs table (bytes / objects / rows, Δ vs previous scan, links to the `/meta` tree), the downstream jobs, the scan's other runs, and the Batch / Cloud Logging links.
- `/meta` is the `meta` secondary store: the scan of the deployments' own data bucket (the treemap at `/meta/<bucket>/<key>`, staff-only, mounted on cw-s3.oa.dev). It is not on gcs.oa.dev, so its base is config: `META_TREE_URL` (e.g. `https://cw-s3.oa.dev/meta`); unset = no meta links. The meta store is itself scanned periodically, so a just-written output appears there after its next meta scan.
- Gate: `requireViewer`, as `/staged`'s run reads.

## Backfill

`dt-cloud scan-run backfill -c job/scan-runs.json` reads, read-only:

- **Batch API** (every job the profile's kinds match, paginated): run id, kind, scan id, status, start / end, machine, SPOT, tasks, image, cost estimate, the failure reason (`Batch: task failed (exit 137)`, `over the maximum runtime`, `cancelled`).
- **Cloud Logging** (30 days): phase markers (each phase's end is its log line's time), the done / NOP markers (scan id, NOP status), the ERR trap's `+ fail_alert <rc> <line> <cmd>` (failed command). A server-side prefilter on each marker's literal head; the full regexes run locally.
- **The data bucket**: each scan's outputs. An index generation (`{gen}`) goes to the run whose window holds its stamp (a REPROC's generation is the REPROC's); every other output to the scan's latest succeeded run among the kinds that write it.

gcs, measured 2026-10-09 (read-only, into a local SQLite file): **242 runs over 73 scans** (2026-07-30 → 2026-10-09T1236) from 517 Batch jobs: 76 scan runs (65 succeeded, 10 failed, 1 running), 48 REPROCs, 118 listing fan-out jobs linked to their parents.

| field | recovered for |
|---|---|
| status, start / end, machine, tasks, cost, Batch failure reason | every run (Batch keeps its jobs) |
| scan id | every run (env, the listing command, else the creation date) |
| phases | runs since 2026-09-09 (Logging's 30-day retention): 33 runs |
| failed command | none in the window (the ERR trap's line is xtrace; recent failures were OOM / max-runtime kills, which never reach the trap) |
| listing (per bucket, rows), dir-cache, snapshot | every scan whose prefixes remain (listing: 62 scans) |
| path-index tiers (+ rows) | scans since 2026-09-07 (earlier generations lived outside `listing/<scan>/index/`) |
| static name index runs | 2026-10-09 (the first appended run) |
| drill / interval store | not per scan yet: no output entry |

## Job glue

### gcs (`job/run.sh`, for the disky-gcs session)

- Check in `job/scan-runs.json` (the profile; the backfill's copy is in this branch's `tmp/glue/gcs-scan-runs.json`, reproduced in the handoff).
- Add `0040_scan_runs.sql` to `site/migrations/gcs/` (the cw file verbatim).
- In `run.sh`, after the scan id and `$GEN` are set:

```bash
# Scan-run record (specs/scan-runs-ui.md): never fatal (`scan-run record` exits 0 on any error).
export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-job/scan-runs.json}
SR_KIND=scan; [ "${REPROC:-0}" = "1" ] && SR_KIND=reproc
scan_run() { { set +x; } 2>/dev/null; [ "${SCRATCH:-0}" = "1" ] || dt-cloud scan-run record -k "$SR_KIND" -g "$GEN" "$@" "$SNAP_ID"; set -x; }
phase() { echo "PHASE $1: ${SECONDS}s (wall${2:+, $2})" >&2; SR_LAST=$1; scan_run -P "$1" ${2:+-q "$2"}; }
scan_run_exit() { local rc=$?; scan_run -x "$rc" ${SR_FAIL:+-F "$SR_FAIL"} ${SR_ERR:+-e "$SR_ERR"}; }
trap scan_run_exit EXIT
scan_run -S -J "$BATCH_JOB_ID_NAME" -R "${BATCH_REGION:-us-central1}"   # -J only if the job name is known
```

  - Replace each `echo "PHASE <name>: ${SECONDS}s (wall…)" >&2` with `phase <name>` (`phase access-ingest "overlapped the listing"`); the marker line is unchanged, so the backfill keeps parsing old and new logs alike.
  - In `fail_alert`, set `SR_ERR="exit $rc: $cmd (run.sh:$line)"` before posting, so the EXIT trap records it.
  - The NOP branch: `scan_run -N` before its `exit 0` (the EXIT trap's `-x 0` then keeps status `nop`? No: `-x` sets `succeeded`, so the NOP branch should `trap - EXIT` after recording).
  - Run `dt-cloud scan-run record` from the image's `dt-cloud` (needs `cloud` merged into `gcs` first).
- After applying the migration, the backfill: `dt-cloud scan-run backfill -c job/scan-runs.json -o` (D1 from `$D1_DB_ID`).

### cw-s3 (`job/cw-run.sh`)

The same helpers; its profile (`job/scan-runs.json` on `cw-s3`) differs in: kinds (`scan` = `commands: job/cw-run.sh`, scan id from `^CW-SCAN-JOB-DONE (\S+)` else the task's start as `%Y-%m-%dT%H%M`), outputs (`snapshots/cw/{scan}/`, `cw-l2/{scan}/` per bucket parquet, `cw-l2/{scan}/index/{gen}/` tiers, the R2 copy under the same keys), prices (`n2-standard-8`). cw-run.sh has no PHASE markers yet: add `phase list-<bucket>`, `phase import-<bucket>`, `phase webdata`, `phase publish`, `phase index-write`, `phase index-sync`, `phase publish-r2`, `phase over-time`, `phase warm`, `phase probe`, `phase digest`. Its migration is this branch's `0016_scan_runs.sql` (cw lineage), applied with `wrangler d1 migrations apply oa-cw-s3-usage-db --remote` after Ryan's go.
