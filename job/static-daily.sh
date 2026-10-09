#!/usr/bin/env bash
# The static name index's daily append for one scan (specs/static-daily-append.md, "Daily entry point"):
#
#   prepare → append (256 ranges, 16 tasks) → shards ∥ catalog → publish (tier merges + manifest) → R2 (runs, then the
#   manifest last) [→ verify] → prune (keep only the newest complete open-version state)
#
#   job/static-daily.sh [-n] DATE
#
# Idempotent and resumable: each stage is skipped when its output is already in the data bucket (prepare:
# `deltas/D/scans.json`; append: all `dhist/` ranges, and `append` itself skips done ranges; shards: `sidecar.parquet`;
# catalog: `catalog/meta.json`; publish: `manifests/D.json`), so a rerun after a failure resumes at the first missing
# one. The R2 copy skips objects already there (same size and md5), so it always runs. Everything is written under new
# keys (the scan's run dir, a merged run dir, `manifests/D.json`); nothing is overwritten. The one delete is the last
# stage's: the scratch bucket's earlier `state/<prev>/` (open versions), once `state/D/` is complete.
#
# Exit status: 0 done (or nothing to do), 3 the scan is not published yet (or is not the next one), else a failure.
# Progress lines (UTC, per stage, with durations) go to stderr and to `tmp/static-daily/D.log`.
#
# -n: dry run. Report each stage's state and the command it would run; submit nothing, write nothing.
#
# Env: GEN (2026-10-08c), R2_VIA (batch: `job/static-names.sh r2-batch`, the R2 key from Secret Manager | vm:
# `job/static-names.sh r2` on the ch-store VM, which replaces /data/sn/src there), VERIFY_TERMS (a gs:// terms file:
# also run `daily verify`), DT_CLOUD (the CLI; default `dt-cloud`), and job/static-names.sh's (SRC, IMAGE, SPOT, …).
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=""
if [ "${1:-}" = -n ]; then DRY=1; shift; fi
D=${1:?usage: job/static-daily.sh [-n] DATE}
GEN=${GEN:-2026-10-08c}
B=oa-gcs-usage-dvx
R2_VIA=${R2_VIA:-batch}
DT=${DT_CLOUD:-dt-cloud}
P=gs://$B/static-names/$GEN
RUN=$P/deltas/$D
export SPOT=${SPOT:-1}

mkdir -p tmp/static-daily
LOG=tmp/static-daily/$D.log
T0=$SECONDS
log() { echo "$(date -u +%FT%TZ) static-daily $D [+$((SECONDS - T0))s] $*" | tee -a "$LOG" >&2; }
exists() { gcloud storage ls "$1" > /dev/null 2>&1; }
count() { gcloud storage ls "$1" 2>/dev/null | wc -l | tr -d ' '; }
# Run a stage's command, or (dry run) say what would run.
stage() {
  local name=$1 t
  shift
  if [ -n "$DRY" ]; then log "$name: would run: $*"; return 0; fi
  log "$name: start: $*"
  t=$SECONDS
  "$@" >> "$LOG.$name.out"
  log "$name: done in $((SECONDS - t))s"
}

log "gen $GEN, R2 via $R2_VIA${DRY:+ (dry run)}"

# The scan has published once its `path` sort is in the bucket.
if ! exists "gs://$B/listing/$D/index/*/path-index.parquet" && ! exists "gs://$B/listing/$D/path-index.parquet"; then
  log "not published: no gs://$B/listing/$D/{index/*/,}path-index.parquet"
  exit 3
fi

# 1. prepare: pin the scan (checks it is the next one after the base and the live runs).
if exists "$RUN/scans.json"; then
  log "prepare: done ($RUN/scans.json)"
elif [ -n "$DRY" ]; then
  log "prepare: would run: $DT static-names daily prepare -g $GEN -d $D"
else
  if ! stage prepare "$DT" static-names daily prepare -g "$GEN" -d "$D"; then
    log "prepare failed: not the next scan? ($LOG.prepare.out)"
    exit 3
  fi
fi

# 2. append: 256 key ranges, 16 per task.
K=$(gcloud storage cat "$P/ranges.json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["k"])')
N=$(count "$RUN/dhist/*.parquet")
if [ "$N" -eq "$K" ]; then
  log "append: done ($N/$K ranges)"
else
  log "append: $N/$K ranges"
  MODULE=static_append JOB_ID=sn-append-$D-$(date -u +%H%M%S) stage append job/static-names.sh run append 16 -g "$GEN" -d "$D" -n $((K / 16))
fi

# 3. shards ∥ catalog (both read only the run's `cdelta/`).
pids=()
if exists "$RUN/sidecar.parquet"; then
  log "shards: done"
else
  MODULE=static_append PARALLELISM=1 JOB_ID=sn-shards-$D-$(date -u +%H%M%S) stage shards job/static-names.sh run shards 1 -g "$GEN" -d "$D" &
  pids+=($!)
fi
if exists "$RUN/catalog/meta.json"; then
  log "catalog: done"
else
  MODULE=static_append JOB_ID=sn-catalog-$D-$(date -u +%H%M%S) stage catalog job/static-names.sh run catalog 1 -g "$GEN" -d "$D" &
  pids+=($!)
fi
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done
if [ "$fail" -ne 0 ]; then log "shards/catalog failed (see $LOG.{shards,catalog}.out)"; exit 1; fi

# 4. publish: tier merges when the binary counter carries (they read the runs: on Batch, with the bucket mounted),
# then `manifests/D.json` (written once, never rewritten).
if exists "$P/manifests/$D.json"; then
  log "publish: done ($P/manifests/$D.json)"
else
  MODULE=static_append PARALLELISM=1 JOB_ID=sn-publish-$D-$(date -u +%H%M%S) stage publish job/static-names.sh run publish 1 -g "$GEN" -d "$D"
fi

# 5. R2: every run the manifest lists (merged runs are new dirs), then the manifest, last.
if [ -n "$DRY" ] && ! exists "$P/manifests/$D.json"; then
  RUNS="deltas/$D (and any merged run publish makes)"
else
  RUNS=$(gcloud storage cat "$P/manifests/$D.json" | python3 -c 'import json, sys; print(" ".join(r["key"] for r in json.load(sys.stdin)["runs"]))')
fi
case $R2_VIA in
  batch) r2() { JOB_ID=sn-r2-$D-$(date -u +%H%M%S) job/static-names.sh r2-batch "$@"; } ;;
  vm) r2() { job/static-names.sh r2 "$@"; } ;;
  *) echo "R2_VIA: batch or vm, not $R2_VIA" >&2; exit 2 ;;
esac
if [ -n "$DRY" ]; then
  log "r2: would copy $RUNS, then $GEN -o manifests/"
else
  for k in $RUNS; do stage "r2-${k//\//-}" r2 "$GEN/$k"; done
  stage r2-manifest r2 "$GEN" -o manifests/
fi

# 6. verify (optional): brute force from the scan file vs base + runs.
if [ -n "${VERIFY_TERMS:-}" ]; then
  if exists "$RUN/verify.json"; then
    log "verify: done"
  else
    MODULE=static_append PARALLELISM=1 JOB_ID=sn-verify-$D-$(date -u +%H%M%S) stage verify job/static-names.sh run verify 1 -g "$GEN" -d "$D" -t "$VERIFY_TERMS"
  fi
fi

# 7. prune: keep only the newest complete open-version state (scratch `state/D/`): every earlier `state/<prev>/` goes.
# `daily prune` refuses (deleting nothing) unless every range's `copen` + `done/` marker and `manifests/D.json` exist;
# a no-op when nothing precedes D. Always runs (dry run: `-n`, which lists what it would delete).
if [ -n "$DRY" ]; then
  log "prune: would run: $DT static-names daily prune -g $GEN -d $D"
  "$DT" static-names daily prune -g "$GEN" -d "$D" -n >&2 || log "prune: not yet (state/$D incomplete)"
else
  if ! stage prune "$DT" static-names daily prune -g "$GEN" -d "$D"; then
    log "prune refused: state/$D incomplete ($LOG.prune.out); if it was lost, rebuild it (daily rebuild-state)"
    exit 1
  fi
fi
log "complete"
