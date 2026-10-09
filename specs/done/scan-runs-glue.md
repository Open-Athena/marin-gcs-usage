# Scan runs: gcs job glue

From the root session's `scan-runs` work (`specs/scan-runs-ui.md` on branch `scan-runs`): record each gcs scan job's run in D1 so `/scans` shows it. Apply on `gcs` after `scan-runs` reaches `cloud` and `cloud` is merged in.

## Steps

1. **Migration** — add `site/migrations/gcs/0040_scan_runs.sql`, a verbatim copy of `site/migrations/cw/0016_scan_runs.sql` (three new tables; nothing existing altered; FKs only between the new tables). Apply to the D1 only on Ryan's go: `wrangler d1 migrations apply oa-gcs-usage-auth --remote`.
2. **Profile** — check in `job/scan-runs.json` (below). It names this deployment's job kinds, phases → outputs, and machine prices; `dt-cloud scan-run` has no defaults.
3. **`job/run.sh`** — apply the patch below (`git apply`): `scan_run` / `phase` helpers, `scan_run -S -B` at start (`-B`: fill machine / SPOT / cost from the Batch job whose uid is `$BATCH_JOB_UID`), `phase <name>` at each phase end (the `PHASE <name>: <s>s` marker line is unchanged, so old and new logs parse alike), an `EXIT` trap recording the exit code, and `fail_alert` handing its `exit <rc>: <cmd> (line)` to that trap. `dt-cloud scan-run record` never fails the job (it prints and exits 0). A SCRATCH or GATE run records nothing; a NOP run records `nop` (`scan_run -N`, which the exit trap keeps). New markers cover the 17-minute tail after `publish` (index-sync, healthcheck, warm-cache, probe, slack-digest, discord-digest, index-gc). The profile's `static` output names the static index generation (`2026-10-08c`): bump it when the generation is rebuilt.
4. **Image** — rebuild (`job/build.sh`): the job needs `dt-cloud scan-run` (on `cloud` from `20749642`).
5. **Backfill** (after the migration, on Ryan's go): `dt-cloud scan-run backfill -c job/scan-runs.json -o` (D1 from `$D1_DB_ID` + the `index-sync` credentials). Read-only against GCP; idempotent; a run the job recorded keeps its own phases.
6. **Site** — `META_TREE_URL = "https://cw-s3.oa.dev/meta"` in `site/wrangler.toml` `[vars]` (both envs) for the outputs' `/meta` tree links; `GCP_PROJECT` is already set (Batch / log links).

## `job/scan-runs.json`

```json
{
  "name": "gcs",
  "comment": "The gcs deployment's scan-run profile (specs/scan-runs-ui.md): `dt-cloud scan-run -c job/scan-runs.json`. Prices: n2 list $/h, us-central1, compute only (vCPU 0.031611 + GB 0.004237).",
  "project": "oa-internal-450019",
  "regions": ["us-central1", "europe-west4"],
  "jobs": [
    {"kind": "reproc", "where": {"entrypoint": "^$", "env": ["REPROC"], "no_env": ["GATE", "SCRATCH", "SNAP_PATH", "SWEEP", "ACCESS_ONLY"]},
     "scan": [{"env": "SNAP_ID"}, {"env": "SNAPSHOT_DATE"}, {"log": "^SNAPSHOT-JOB-DONE (\\S+)"}]},
    {"kind": "scan", "where": {"name": "^(job-|gcs-usage-snapshot)", "image": "gcs-usage-snapshot(:latest|@sha256:)", "entrypoint": "^$",
                              "no_env": ["REPROC", "GATE", "SCRATCH", "SNAP_PATH", "SWEEP", "ACCESS_ONLY", "SWEEP_BENCHMARK_PLAN"]},
     "scan": [{"env": "SNAP_ID"}, {"env": "SNAPSHOT_DATE"}, {"log": "^SNAPSHOT-JOB-(?:DONE|NOP) (\\S+)"}, {"created": "%Y-%m-%d"}]},
    {"kind": "listing", "downstream": true, "where": {"commands": "disk-tree bulk-list \"gcs://"},
     "scan": [{"commands": "/listing/(\\d{4}-\\d{2}-\\d{2}(?:T\\d{4})?)/\\$b"}]}
  ],
  "overlapped": ["access-ingest"],
  "nop_marker": "^SNAPSHOT-JOB-NOP ",
  "error_marker": "^\\+ fail_alert (?P<rc>\\d+) (?P<line>\\d+) (?P<cmd>.*)",
  "outputs": [
    {"key": "listing", "uri": "gs://oa-gcs-usage-dvx/listing/{scan}/", "exclude": ["dir-cache/", "index/"], "split": "dir",
     "rows": {"json": "_SUCCESS.json", "field": "objects"}, "after": "listing-fanout", "kinds": ["scan"]},
    {"key": "dir-cache", "uri": "gs://oa-gcs-usage-dvx/listing/{scan}/dir-cache/", "after": "path-index", "kinds": ["scan"]},
    {"key": "path-index", "uri": "gs://oa-gcs-usage-dvx/listing/{scan}/index/{gen}/", "split": "stem", "rows": "groups", "after": "path-index upload"},
    {"key": "snapshot", "uri": "gs://oa-gcs-usage-dvx/snapshots/{scan}/", "after": "publish"},
    {"key": "static", "uri": "gs://oa-gcs-usage-dvx/static-names/2026-10-08c/manifests/{scan}.json", "manifest": true, "kinds": ["scan"]}
  ],
  "prices": {"n2-highmem-32": 2.0962, "n2-highmem-16": 1.0481, "n2-highmem-8": 0.5241, "n2-standard-32": 1.5539, "n2-standard-8": 0.3885},
  "spot_factor": 0.3,
  "log_days": 30
}
```

## `job/run.sh` patch

```diff
--- a/job/run.sh	2026-10-09 08:32:03
+++ b/job/run.sh	2026-10-09 10:15:42
@@ -81,6 +81,27 @@
 INDEX_DIR=listing/$SNAP_ID/index/$GEN
 INDEX_PATH=${INDEX_PATH:-$INDEX_DIR/path-index.parquet}
 
+# Scan-run record (specs/scan-runs-ui.md): this run's phases, outputs and exit
+# in D1 (`scan_runs`), shown at /scans. `dt-cloud scan-run record` never fails
+# the job (it prints and exits 0 on any error) and writes over the D1 HTTP API
+# with the index-sync credentials, so xtrace is off around it. The profile
+# (`job/scan-runs.json`) says what each phase wrote. A SCRATCH / GATE run is
+# not a scan run: nothing is recorded.
+export SCAN_RUNS_PROFILE=${SCAN_RUNS_PROFILE:-job/scan-runs.json}
+SR_KIND=scan; [ "${REPROC:-0}" = "1" ] && SR_KIND=reproc
+SR_ERR=""
+scan_run() {
+  { set +x; } 2>/dev/null
+  if [ "${SCRATCH:-0}" != "1" ] && [ "${GATE:-0}" != "1" ] && [ -n "${BATCH_JOB_UID:-}" ]; then
+    (cd /app && dt-cloud scan-run record -k "$SR_KIND" -g "$GEN" "$@" "$SNAP_ID")
+  fi
+  set -x
+}
+# A phase just ended: the marker line (the backfill parses these from the
+# logs) and its record. `phase NAME [NOTE]`.
+phase() { echo "PHASE $1: ${SECONDS}s (wall${2:+, $2})" >&2; scan_run -P "$1" ${2:+-q "$2"}; }
+scan_run_exit() { local rc=$?; scan_run -x "$rc" ${SR_ERR:+-e "$SR_ERR"}; exit "$rc"; }
+
 # Failure alerting: any command dying under `set -e` posts to Slack before the
 # job exits — otherwise silence is the only failure signal (the success digest
 # is the very last step, so a hard failure posts nothing; both 2026-08-28
@@ -115,6 +136,7 @@
 }
 fail_alert() {
   local rc=$1 line=$2 cmd=$3
+  SR_ERR="exit $rc: $cmd (run.sh:$line)"  # the EXIT trap records it (scan_run_exit)
   local msg="❌ \`dt-cloud\` snapshot job failed ($SNAP_ID): \`${cmd}\` exited $rc at run.sh:$line"
   [ -n "${BATCH_JOB_UID:-}" ] && msg+=$'\n'"<https://console.cloud.google.com/logs/query;query=labels.job_uid%3D%22$BATCH_JOB_UID%22?project=oa-internal-450019|task logs>"
   slack_post "$msg"
@@ -187,6 +209,11 @@
   exit 0
 fi
 
+# From here on this job is a scan run (the branches above exit as other jobs):
+# record its start (with its Batch facts: machine, SPOT, cost) and its exit.
+trap scan_run_exit EXIT
+scan_run -S -B
+
 # Scheduled retry attempts set NOP_IF_PUBLISHED=1: exit quietly when the
 # snapshot already exists (an earlier attempt won). Manual runs leave it
 # unset so intentional re-runs always proceed. A NOP retry still ingests access
@@ -195,6 +222,7 @@
   [ "${SKIP_ACCESS:-0}" = "1" ] || dt-cloud access ingest ${ACCESS_ARGS:-} \
     || echo "WARN: access ingest failed (watermark self-heals next run)" >&2
   echo "SNAPSHOT-JOB-NOP $SNAP_ID (already published)"
+  scan_run -N
   exit 0
 fi
 
@@ -235,14 +263,14 @@
   LZ+=(-L marin-us-central2=central2-listing)
   dt-cloud job submit-listing -d "$SNAP_ID" -W "${LZ[@]}"
 fi
-echo "PHASE listing-fanout: ${SECONDS}s (wall)" >&2
+phase listing-fanout
 
 # Barrier: the concurrent access ingest must finish before we stage (stage reads
 # its access/agg shards) and before HAVE_ACCESS is computed just below. Non-fatal.
 if [ -n "$ACCESS_PID" ]; then
   wait "$ACCESS_PID" || echo "WARN: access ingest failed (snapshot continues; watermark self-heals next run)" >&2
 fi
-echo "PHASE access-ingest: ${SECONDS}s (wall, overlapped the listing)" >&2
+phase access-ingest "overlapped the listing"
 G=()
 for b in "${FLEET[@]}"; do
   if [ -f "/gcs/$DATA/listing/$SNAP_ID/$b/_SUCCESS.json" ]; then G+=("/gcs/$DATA/listing/$SNAP_ID/$b/*.parquet")
@@ -323,7 +351,7 @@
   -c "/gcs/$DATA/listing/$SNAP_ID/dir-cache" \
   -P "$PI_DIR/path-index.parquet" ${PATH_INDEX_RG_ROWS:+-r "$PATH_INDEX_RG_ROWS"} -u "${PATH_INDEX_USER_SORT_TIERS:-bysize}" \
   ${PATH_INDEX_SEARCH:+-S}  # PATH_INDEX_SEARCH=1: the filter's search sidecars too (specs/path-store-search.md; off until measured)
-echo "PHASE path-index: ${SECONDS}s (wall)" >&2
+phase path-index
 ls -l "$PI_DIR" >&2
 PI_DIR="$PI_DIR" DATA="$DATA" DEST="$(dirname "$INDEX_PATH")" python3 - <<'PY'
 import os, time
@@ -342,9 +370,9 @@
     tot += f.stat().st_size
 print(f"path-index upload: {tot / 1e9:.1f} GB in {time.time() - t0:.0f}s → gs://{os.environ['DATA']}/{dest}/", flush=True)
 PY
-echo "PHASE path-index upload: ${SECONDS}s (wall)" >&2
+phase "path-index upload"
 dt-cloud rules -o /tmp/rules.json || true  # findings shouldn't block the snapshot
-echo "PHASE webdata+stage: ${SECONDS}s (wall)" >&2
+phase webdata+stage
 
 # 3. publish to the canonical store — the live site reads these directly
 # (site/functions/data/[[path]].ts), so no site rebuild/deploy is needed.
@@ -363,7 +391,7 @@
 fi
 # attribution rules: the single latest copy the /data/rules.json function serves
 cp /tmp/rules.json "/gcs/$DATA/snapshots/rules.json" 2>/dev/null || true
-echo "PHASE publish: ${SECONDS}s (wall)" >&2
+phase publish
 
 # Footer-in-D1: sync the path-index parquet footer into the site's D1 so the
 # reader skips the cold-isolate footer parse (specs/done/path-agnostic-serving.md
@@ -379,6 +407,7 @@
   echo "WARN: no CLOUDFLARE_API_TOKEN/ACCOUNT_ID — skipping index-sync (scan stays unlisted)" >&2
 fi
 set -x
+phase index-sync
 
 # Optional (off unless CH_STORE_URL is set): append this scan to the ClickHouse
 # store the serving box answers from (specs/ch-store.md §3). `ch-ingest` streams
@@ -389,7 +418,7 @@
 # CLICKHOUSE_USER / CLICKHOUSE_PASSWORD (secrets) authenticate, if set.
 if [ -n "${CH_STORE_URL:-}" ]; then
   if CLICKHOUSE_URL=$CH_STORE_URL dt-cloud ch-ingest -d "$SNAP_ID" "$PI_DIR/path-index.parquet"; then
-    echo "PHASE ch-ingest: ${SECONDS}s (wall)" >&2
+    phase ch-ingest
   else
     echo "WARN: ch-ingest failed for $SNAP_ID (re-run: dt-cloud ch-ingest -d $SNAP_ID)" >&2
   fi
@@ -413,6 +442,8 @@
   fi
 fi
 
+phase healthcheck
+
 # Warm the site's subtree + diff caches for this scan (colo cache + global
 # KV) so the first viewer of the day gets hits instead of a multi-second
 # compute — the home page's default requests at the common canvas widths
@@ -422,6 +453,8 @@
     || echo "WARN: cache warm-up failed for $SNAP_ID" >&2
 fi
 
+phase warm-cache
+
 # Replay the site's page loads uncached (`dt-cloud probe -c`): every response
 # a 2xx/4xx, and one cold-latency record per scan under probes/ (the time
 # series of what a first viewer waits). Same token, same gating; a failure
@@ -433,6 +466,8 @@
   fi
 fi
 
+phase probe
+
 # Converge the monthly Shape-C digest thread in Slack (specs/done/slack-digest-
 # shape-c.md): the OP + one reply per scan. Only when SLACK_BOT_TOKEN +
 # SLACK_CHANNEL are set — Shape C needs the Web API's per-message sender/avatar
@@ -449,6 +484,8 @@
   echo "no Slack bot transport (SLACK_BOT_TOKEN+SLACK_CHANNEL) — skipping usage digest" >&2
 fi
 
+phase slack-digest
+
 # The same month thread in Marin's Discord #gcs-usage (specs/done/discord-
 # digest-twin.md): OP + plot through the channel's app-owned webhook, the bot
 # opens the thread and lends its arrow emoji. Both secrets are always mounted
@@ -462,6 +499,8 @@
   echo "no Discord transport (DISCORD_GCS_USAGE_WEBHOOK+DISCORD_BOT_TOKEN) — skipping Discord digest" >&2
 fi
 
+phase discord-digest
+
 # Sweep row groups of generations the pointer no longer names (a REPROC's
 # previous generation; every reader handle has expired by now), and retire
 # the floor-free row groups of scans older than the newest INDEX_RETAIN —
@@ -475,6 +514,7 @@
   dt-cloud index-gc -r "${INDEX_RETAIN:-30}" "$SNAP_ID" || echo "WARN: index-gc failed" >&2
 fi
 set -x
+phase index-gc
 
 echo "PHASE total: ${SECONDS}s (wall)" >&2
 echo "SNAPSHOT-JOB-DONE $SNAP_ID"
```
