# Scan runs: cw-s3 job glue

From the root session's `scan-runs` work (`specs/scan-runs-ui.md` on branch `scan-runs`): record each cw-s3 scan job's run in D1 so `/scans` shows it. Apply on `cw-s3` after `scan-runs` reaches `cloud` and `cloud` is merged in.

## Status (2026-10-09): done

All six steps are applied on `cw-s3`:
- `cloud` merged (`a2016a84`); the glue landed as written: profile, the patch applied cleanly, and `META_TREE_URL` in both envs.
- `0016_scan_runs.sql` was checked on a fresh SQLite with `foreign_keys=ON` (FK check clean), then applied to the prod D1.
- Backfill: a dry run into local SQLite, with its Batch/log inputs saved (`-w`), then written to D1 by replaying them (`-j`/`-l`). 145 runs over 145 scans (130 succeeded, 15 failed) and 1,055 output rows. There are no phases, since no past cw run logged phase markers.
- Deployed to dev, CIC'd (`/scans`, `/scans/<run>`), then to prod (`cw-s3-prod` `1474262e`). The `:cw` image is rebuilt, so the next scan records its own run, phases included.

## Steps

1. **Migration** — the cw lineage's `site/migrations/cw/0016_scan_runs.sql` arrives with the `cloud` merge. Apply to the D1 only on Ryan's go: `wrangler d1 migrations apply oa-cw-s3-usage-db --remote`.
2. **Profile** — check in `job/scan-runs.json` (below). It names this deployment's job kinds, phases → outputs, and machine prices; `dt-cloud scan-run` has no defaults.
3. **`job/cw-run.sh`** — apply the patch below (`git apply`): `scan_run` / `phase` helpers, `scan_run -S -B` at start (`-B`: fill machine / SPOT / cost from the Batch job whose uid is `$BATCH_JOB_UID`), `phase <name>` at each phase end (the `PHASE <name>: <s>s` marker line is unchanged, so old and new logs parse alike), an `EXIT` trap recording the exit code, and `fail_alert` handing its `exit <rc>: <cmd> (line)` to that trap. `dt-cloud scan-run record` never fails the job (it prints and exits 0). `GEN` moves up to the top (stamped once at start) so every record names it. cw-run.sh had no phase markers: this adds `bucket-check`, `list <bucket>` / `import <bucket>` per bucket, `webdata`, `publish`, `layer-2`, `index-write`, `index-sync`, `publish-r2`, `over-time`, `index-gc`, `warm-cache`, `probe`, `digest`. A dry backfill (local SQLite) of cw recovered 145 runs over 145 scans (130 succeeded, 15 failed; 2026-08-19 → 2026-10-09T1201), 123 with layer-2 + snapshot outputs and 53 with index tiers. The R2 copies (same keys) are not measured separately.
4. **Image** — rebuild (`job/build.sh`): the job needs `dt-cloud scan-run` (on `cloud` from `20749642`).
5. **Backfill** (after the migration, on Ryan's go): `dt-cloud scan-run backfill -c job/scan-runs.json -o` (D1 from `$D1_DB_ID` + the `index-sync` credentials). Read-only against GCP; idempotent; a run the job recorded keeps its own phases.
6. **Site** — `META_TREE_URL = "https://cw-s3.oa.dev/meta"` in `site/wrangler.toml` `[vars]` (both envs) for the outputs' `/meta` tree links; `GCP_PROJECT` is already set (Batch / log links).

## `job/scan-runs.json`

```json
{
  "name": "cw",
  "comment": "The cw-s3 deployment's scan-run profile (specs/scan-runs-ui.md): `dt-cloud scan-run -c job/scan-runs.json`. Prices: n2 list $/h, us-central1, compute only.",
  "project": "oa-internal-450019",
  "regions": ["us-central1"],
  "jobs": [
    {"kind": "scan", "where": {"commands": "^job/cw-run\\.sh"},
     "scan": [{"env": "SNAP_ID"}, {"log": "^CW-SCAN-JOB-DONE (\\S+)"}, {"started": "%Y-%m-%dT%H%M"}]}
  ],
  "error_marker": "^\\+ fail_alert (?P<rc>\\d+) (?P<line>\\d+) (?P<cmd>.*)",
  "outputs": [
    {"key": "layer-2", "uri": "gs://oa-gcs-usage-dvx/cw-l2/{scan}/", "exclude": ["index/"], "split": "stem", "after": "layer-2"},
    {"key": "path-index", "uri": "gs://oa-gcs-usage-dvx/cw-l2/{scan}/index/{gen}/", "split": "stem", "rows": "groups", "after": "index-write"},
    {"key": "snapshot", "uri": "gs://oa-gcs-usage-dvx/snapshots/cw/{scan}/", "after": "publish"},
    {"key": "static", "uri": "gs://oa-gcs-usage-dvx/static-names/2026-10-09cw/manifests/{scan}.json", "manifest": true}
  ],
  "prices": {"n2-standard-8": 0.3885, "n2-standard-16": 0.777, "n2-highmem-16": 1.0481},
  "spot_factor": 0.3,
  "log_days": 30
}
```

## `job/cw-run.sh` patch

```diff
--- a/job/cw-run.sh	2026-10-08 03:32:28
+++ b/job/cw-run.sh	2026-10-09 10:18:00
@@ -71,6 +71,24 @@
 # evening during a cleanup push, so a date-only id would silently overwrite.
 SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}
 DATE=${SNAP_ID%%T*}
+GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}  # this run's index generation (step 4b), stamped once at start
+
+# Scan-run record (specs/scan-runs-ui.md): this run's phases, outputs and exit
+# in D1 (`scan_runs`), shown at /scans. `dt-cloud scan-run record` never fails
+# the job and writes over the D1 HTTP API (the index-sync credentials), so
+# xtrace is off around it. `job/scan-runs.json` says what each phase wrote.
+export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-job/scan-runs.json}
+SR_ERR=""
+SECONDS=0
+scan_run() {
+  { set +x; } 2>/dev/null
+  if [ "${REPROC:-0}" != "1" ] && [ -n "${BATCH_JOB_UID:-}" ]; then
+    (cd /app && dt-cloud scan-run record -g "$GEN" "$@" "$SNAP_ID")
+  fi
+  set -x
+}
+phase() { echo "PHASE $1: ${SECONDS}s (wall${2:+, $2})" >&2; scan_run -P "$1" ${2:+-q "$2"}; }
+scan_run_exit() { local rc=$?; scan_run -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"; }
 
 # Failure alerting: any command dying under `set -e` posts to Slack before the
 # job exits (silence was the only failure signal — the 8/26–8/28 crash-loop on
@@ -93,11 +111,14 @@
 fail_alert() {
   local rc=$1 line=$2 cmd=$3
   { set +x; } 2>/dev/null
+  SR_ERR="exit $rc: $cmd (cw-run.sh:$line)"  # the EXIT trap records it (scan_run_exit)
   local msg="❌ CoreWeave scan job failed ($SNAP_ID): \`${cmd}\` exited $rc at cw-run.sh:$line"
   [ -n "${BATCH_JOB_UID:-}" ] && msg+=$'\n'"<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22$BATCH_JOB_UID%22?project=oa-internal-450019|task logs>"
   slack_alert "$msg"
 }
 trap 'fail_alert $? $LINENO "$BASH_COMMAND"' ERR
+trap scan_run_exit EXIT
+scan_run -S -B
 
 # Every bucket the CAIOS key can list is either scanned or empty. A non-empty
 # one outside SCAN_BUCKETS (a new zone's bucket) is billed storage the site
@@ -127,6 +148,7 @@
 print(" ".join(out))
 PY
 )
+phase bucket-check
 if [ -n "$UNSCANNED" ]; then
   echo "WARN: unscanned-bucket check: $UNSCANNED" >&2
   { set +x; } 2>/dev/null
@@ -155,14 +177,17 @@
 for b in $BUCKETS; do
   disk-tree bulk-list -a "s3://$b" -E "$ENDPOINT" \
     -o "$WORK/listing/$b" -P "$PROCS" -w "$WORKERS" -x clear
+  phase "list $b"
   env DISK_TREE_ROOT="$WORK/l2/$b" \
     disk-tree import -e stream -j "${IMPORT_JOBS:-8}" -m -p storage_class_id \
       -s s3 -t "${DATE}T00:00:00+00:00" -l "$WORK/listing/$b/shard-*.parquet" -b "$b"
   SRC+=("$b=$(ls "$WORK/l2/$b"/scans/*.parquet | head -1)")
+  phase "import $b"
 done
 
 # 3. Site JSONs: the store root wraps one node per bucket.
 python job/cw-webdata.py "$WORK/web" "${SRC[@]}" -l "Marin CoreWeave" -a "$DATE"
+phase webdata
 
 # 4. Publish. Written last and all at once: the site's scan list is derived by
 # listing this prefix, so a partially-uploaded snapshot would show up in the
@@ -177,11 +202,13 @@
 # lifecycle diff|push`); a pull never fails the scan.
 LB=(); for b in $BUCKETS; do LB+=(-b "$b"); done
 dt-cloud lifecycle pull "${LB[@]}" -o "$DEST/lifecycle.json" || echo "WARN: lifecycle pull failed" >&2
+phase publish
 
 # Keep the canonical layer-2 parquets too -- they're the input to every ad-hoc
 # question ("what grew?", "what's idle?") that the JSONs can't answer.
 mkdir -p "/gcs/$DATA/cw-l2/$SNAP_ID"
 for s in "${SRC[@]}"; do cp "${s#*=}" "/gcs/$DATA/cw-l2/$SNAP_ID/${s%%=*}.parquet"; done
+phase layer-2
 
 # 4b. Index tiers (specs/view-serving.md, ported from gcs): the site's
 # /api/subtree, /api/diff and /api/series read row-group-pruned ranges of
@@ -190,11 +217,11 @@
 # Each run writes its tiers under a fresh generation dir and never overwrites
 # a file D1 points at; `index-sync` lands the footers in D1 and flips the
 # pointer last, so a reader sees the old complete set or the new one.
-GEN=${GEN:-$(date -u +%Y%m%dT%H%M%SZ)}
 INDEX_KEY="cw-l2/$SNAP_ID/index/$GEN"
 dt-cloud index-write -m "${DUCKDB_MEM:-16GB}" -t "${IMPORT_JOBS:-8}" -o "$WORK/index" "${SRC[@]}"
 mkdir -p "/gcs/$DATA/$INDEX_KEY"
 cp "$WORK"/index/*.parquet "/gcs/$DATA/$INDEX_KEY/"  # path-index + coarse tiers + age-index
+phase index-write
 if [ -n "${CLOUDFLARE_API_TOKEN:+set}" ] && [ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then  # `:+set`: xtrace must not print the token
   dt-cloud index-sync -d "/gcs/$DATA/$INDEX_KEY" -g "$GEN" -k "$INDEX_KEY" "$SNAP_ID" \
     || echo "WARN: index-sync failed (the site keeps serving the previous generation)" >&2
@@ -202,6 +229,7 @@
 else
   echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — tiers published but not synced (scan unlisted for the index reader)" >&2
 fi
+phase index-sync
 
 # 4c. Publish this scan's served subset to R2 (specs/done/r2-serving-migration.md):
 # the snapshot JSONs + the layer-2 dir (parquets, index tiers, manifests) the
@@ -219,6 +247,7 @@
 else
   echo "no R2_ENDPOINT/R2_BUCKET — skipping publish-r2" >&2
 fi
+phase publish-r2
 
 # 4d. Over-time index groups (specs/obs-axis-indexing.md Phase 1): every full
 # K-scan run of indexed scans not yet in the manifest becomes one sealed
@@ -227,6 +256,7 @@
 # backfill via cw-overtime-submit.sh); it takes this run's env. Never fatal:
 # the series keeps its per-scan reads if a group fails.
 GEN="$GEN" WORK="$WORK/over-time" bash job/cw-overtime.sh || echo "WARN: over-time groups failed (the series keeps its per-scan reads)" >&2
+phase over-time
 
 # 4e. Delete superseded index generations' files (specs/storage-consolidation.md
 # phase 1): `index-gc` above drops only their D1 rows, so every reindex used to
@@ -242,6 +272,7 @@
   dt-cloud index-gc -R -m 2d "${GC_TARGETS[@]}" \
     || echo "WARN: index-gc -F failed (superseded generations stay until the next run)" >&2
 fi
+phase index-gc
 
 # 4b. Warm the site's subtree + diff caches for this scan (the colo cache, plus
 # the global KV tier once `CACHE_KV` is bound in site/wrangler.toml) so the
@@ -259,6 +290,7 @@
 else
   echo "no warm-cache auth (SITE_TOKEN, or CF_ACCESS_CLIENT_ID+SECRET) — skipping cache warm-up" >&2
 fi
+phase warm-cache
 
 # 4c'. Replay the site's page loads uncached (`dt-cloud probe -c`, as the gcs
 # job does): every response a 2xx/4xx, and one cold-latency record per scan
@@ -268,6 +300,7 @@
   dt-cloud probe -c -u "$SITE_URL" -o "gs://$DATA/probes/cw/" \
     || echo "WARN: serving probe failed for $SNAP_ID (a 5xx or a dropped request; data is fine)" >&2
 fi
+phase probe
 
 # 5. Converge the monthly Shape-C digest thread in Slack (specs/cw-slack-
 # digest.md): the OP + one reply per UTC day (+ the day's provisional reply
@@ -284,5 +317,6 @@
 else
   echo "no Slack bot transport (SLACK_BOT_TOKEN+SLACK_CHANNEL) — skipping usage digest" >&2
 fi
+phase digest
 
 echo "CW-SCAN-JOB-DONE $SNAP_ID -> gs://$DATA/snapshots/cw/$SNAP_ID"
```
