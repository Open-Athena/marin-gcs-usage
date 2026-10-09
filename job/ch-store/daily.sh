#!/bin/bash
# On the ch-store VM (as root; the `ch-daily` systemd timer, or by hand): bring
# the consolidated store up to the newest scan gcs has published, one date at a
# time, oldest first. Per date:
#   1. `ingest-days.sh` (`ch-ingest -F`): the scan's newest path-store generation into `default`;
#   2. `ch-mega-names-append`: its opened versions and closures into each name
#      postings stem (`DAILY_POSTINGS`) and `name_spans`;
#   3. `ch-mega-catalog-build -e DATE`: the catalog stem (`DAILY_CATALOG`)
#      appended through it;
# then `serve-query` restarts once, so the new dates are served (it reads the
# covered dates at startup).
#
# Every step is a no-op once done (ingest is idempotent; the name and catalog
# stems are skipped where `name_index_log` already covers the date), so a
# failed run is just rerun. One run at a time (flock on /data/daily.lock), and
# none while a `ch-job-*` container (a heavy `py-bg` job) runs.
# Config: /data/daily.env (DAILY_POSTINGS, DAILY_CATALOG, DAILY_CENSUS_BIN,
# DAILY_DELTA_BIN; optional DAILY_SRC, DAILY_THREADS, DAILY_BUCKET).
# Records: /data/daily/<date>.jsonl; log: /data/daily/daily.log.
#   daily.sh        # catch up
#   daily.sh -n     # print the dates and steps it would run, change nothing
set -euo pipefail
DRY=0
[ "${1:-}" = -n ] && DRY=1
# shellcheck source=/dev/null
. /data/daily.env
: "${DAILY_POSTINGS:?}" "${DAILY_CATALOG:?}" "${DAILY_CENSUS_BIN:?}" "${DAILY_DELTA_BIN:?}"
SRC=${DAILY_SRC:-/data/src}
THREADS=${DAILY_THREADS:-32}
B=gs://${DAILY_BUCKET:-oa-gcs-usage-dvx}/listing
IMAGE=$(cat /data/image)
OUT=/data/daily
mkdir -p "$OUT"
exec 9> /data/daily.lock
flock -n 9 || { echo "daily.sh: another run holds /data/daily.lock" >&2; exit 0; }

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$OUT/daily.log" >&2; }
q() { clickhouse-client -q "$1"; }
cli() { docker run --rm --network host -v /data:/data -e PYTHONPATH="$SRC" --entrypoint python3 "$IMAGE" -u -m dt_cloud.cli "$@"; }
# Whether `name_index_log` covers DATE for STEM.
# A failed query stops the run, never reads as "not covered" (`set -e` doesn't
# apply inside a function called as an `if` condition, hence the explicit exit).
covered() {
  local n
  n=$(q "SELECT count() FROM default.name_index_log WHERE stem = '$1' AND through >= toDateTime('$2', 'UTC')") || { log "query failed; stopping"; exit 1; }
  [ "$n" != 0 ]
}
# The newest generation's `path` sort for DATE, or nothing while gcs hasn't published one.
published() { gcloud storage ls "$B/$1/index/*/path-index.parquet" 2>/dev/null | sort | tail -1; }

# Never overlap a heavy job (`job/ch-store.sh py-bg` containers): defer to the next run.
heavy=$(docker ps --filter name='^ch-job-' --format '{{.Names}}' | tr '\n' ' ')
[ -z "$heavy" ] || { log "deferring: heavy job(s) running: $heavy"; exit 0; }
latest=$(q "SELECT toDate(max(vf)) FROM default.nodes")
[ -n "$latest" ]
mapfile -t dates < <(gcloud storage ls "$B/" | sed -nE 's|.*/listing/([0-9]{4}-[0-9]{2}-[0-9]{2})/$|\1|p' | awk -v l="$latest" '$1 > l' | sort)
todo=()
for d in "${dates[@]}"; do
  [ -n "$(published "$d")" ] || { log "$d: no path-index published yet; stopping before it"; break; }
  todo+=("$d")
done
# Dates `default` already holds but a stem doesn't cover yet (a run that failed after ingest).
# shellcheck disable=SC2086  # DAILY_POSTINGS is a word list
stems="'$DAILY_CATALOG', $(printf "'%s'," $DAILY_POSTINGS | sed 's/,$//')"
# The stem furthest behind: each stem's newest covered scan, then the oldest of those.
behind_q="SELECT DISTINCT toString(toDate(vf)) FROM default.nodes WHERE vf > (SELECT min(t) FROM (SELECT max(through) AS t FROM default.name_index_log WHERE stem IN ($stems) GROUP BY stem)) ORDER BY 1"
behind=$(q "$behind_q")
mapfile -t behind <<< "$behind"
mapfile -t todo < <(printf '%s\n' "${behind[@]}" "${todo[@]}" | sed '/^$/d' | sort -u)
log "store through $latest; to do: ${todo[*]:-nothing}"
[ "${#todo[@]}" -gt 0 ] || exit 0

for d in "${todo[@]}"; do
  rec="$OUT/$d.jsonl"
  if [ "$d" \> "$latest" ]; then
    log "$d: ingest $(published "$d")"
    # ingest-days.sh stages the parquet into ClickHouse's user_files and reads it server-side (`-F`).
    [ "$DRY" = 1 ] || { THREADS=$THREADS /data/ingest-days.sh "$d" && tail -1 /data/ingest.jsonl >> "$rec"; }
  fi
  for stem in $DAILY_POSTINGS; do
    if covered "$stem" "$d"; then continue; fi
    log "$d: names → $stem"
    [ "$DRY" = 1 ] || cli ch-mega-names-append -t "$THREADS" "$d" "$stem" >> "$rec"
  done
  if ! covered "$DAILY_CATALOG" "$d"; then
    log "$d: catalog → $DAILY_CATALOG"
    [ "$DRY" = 1 ] || cli ch-mega-catalog-build -c "$DAILY_CENSUS_BIN" -d "$DAILY_DELTA_BIN" -t "$THREADS" -e "$d" -P "${DAILY_POSTINGS%% *}" "$DAILY_CATALOG" >> "$rec"
  fi
  log "$d: done"
done
if [ "$DRY" = 0 ]; then
  docker restart serve-query > /dev/null
  log "serve-query restarted"
fi
