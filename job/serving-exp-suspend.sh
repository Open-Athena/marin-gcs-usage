#!/usr/bin/env bash
# Experiment A′ (specs/serving-options.md): the `mem` index on a GCE VM that is
# suspended and resumed (memory kept), against a stop/start that reloads the
# index from the boot PD. Run from a laptop; the VM runs `dt-cloud bench-serve`
# in the gcs job image, and every timing is taken here, from the API call.
#
#   job/serving-exp-suspend.sh create        # VM + startup script (docker, image, src, cold load from GCS)
#   job/serving-exp-suspend.sh ready         # wait for /health; print the load stats
#   job/serving-exp-suspend.sh run [id…]     # GET /run (all 105, or these ids) → JSON
#   job/serving-exp-suspend.sh cycle SECONDS # suspend, wait, resume; time each step + first query + all 105
#   job/serving-exp-suspend.sh stopstart     # stop, start, reload from PD (page cache cold); same timings
#   job/serving-exp-suspend.sh delete        # VM + disk, then verify they're gone
#
# Env: VM (serving-exp-a), ZONE (us-east1-c), MACHINE (n2-highmem-8), DISK_TYPE (pd-ssd), DISK_GB (500),
# FIRST (the first query after a resume: safetensors). Records append to tmp/serving-exp/a-*.jsonl.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
VM=${VM:-serving-exp-a}
ZONE=${ZONE:-us-east1-c}
MACHINE=${MACHINE:-n2-highmem-8}
DISK_TYPE=${DISK_TYPE:-pd-ssd}
DISK_GB=${DISK_GB:-500}
FIRST=${FIRST:-safetensors}
B=oa-gcs-usage-dvx
X=gs://$B/scratch/bench/serving-exp
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
GEN=gs://$B/listing/2026-10-01/index/20261002T113031Z
INDEX=$X/mem-index/2026-10-01
Q=gs://$B/scratch/bench/queries/gcs.yml
T=gs://$B/scratch/bench/truth/2026-10-01/
OUT=tmp/serving-exp
mkdir -p $OUT
G=(--project "$PROJECT" --zone "$ZONE")

now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
since() { python3 -c "import sys; print(round(float(sys.argv[2]) - float(sys.argv[1]), 2))" "$1" "$2"; }
ip() { gcloud compute instances describe "$VM" "${G[@]}" --format='value(networkInterfaces[0].accessConfigs[0].natIP)'; }
status() { gcloud compute instances describe "$VM" "${G[@]}" --format='value(status)'; }
# Plain ssh (keys pushed by one `gcloud compute ssh`): no gcloud start-up in the timed path.
vssh() { ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=3 -o BatchMode=yes -i ~/.ssh/google_compute_engine "$USER@$IP" "$@"; }
get() { vssh curl -sf "http://localhost:8765$1"; }

# The container that serves: `$1` = index (gs:// → copied to the PD first), `$2` extra flags.
serve_cmd() {
  echo "sudo docker rm -f serve >/dev/null 2>&1; sudo docker run -d --name serve --network host -v /data:/data -e PYTHONPATH=/data/src \
    --entrypoint python3 $IMAGE -u -m dt_cloud.cli bench-serve -e mem -i $1 -s /data/stage -t 8 $2 -Q $Q -T $T $GEN"
}

case ${1:?create|ready|run|cycle|stopstart|delete} in
create)
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$X/src/dt_cloud" >&2
  startup=$(mktemp)
  cat > "$startup" <<EOF
#!/bin/bash
# first boot only: docker, the image, the staged source; then serve (cold load from GCS)
[ -f /data/.setup ] && exit 0
mkdir -p /data/stage /data/src
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io > /dev/null
gcloud auth configure-docker us-central1-docker.pkg.dev -q
docker pull -q $IMAGE
gcloud storage cp -r $X/src/dt_cloud /data/src/
touch /data/.setup
$(serve_cmd "$INDEX/" "")
EOF
  gcloud compute instances create "$VM" "${G[@]}" --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud --boot-disk-type "$DISK_TYPE" --boot-disk-size "${DISK_GB}GB" \
    --service-account "gcs-usage-job@$PROJECT.iam.gserviceaccount.com" --scopes cloud-platform \
    --labels purpose=serving-exp --metadata-from-file startup-script="$startup"
  rm -f "$startup"
  sleep 20
  gcloud compute ssh "$VM" "${G[@]}" --command true -- -o StrictHostKeyChecking=no >&2
  ;;
sh)  # a command on the VM
  IP=$(ip)
  vssh "$2"
  ;;
ready)
  IP=$(ip)
  until get /health > $OUT/a-health.json 2>/dev/null; do sleep 10; done
  cat $OUT/a-health.json
  ;;
run)
  IP=$(ip)
  shift
  qs=$(printf '&k=%s' "$@")
  get "/run?r=1${qs}"
  ;;
cycle)
  wait_s=${2:?seconds suspended}
  IP=$(ip)
  base=$(get "/run?k=$FIRST")
  t0=$(now)
  gcloud compute instances suspend "$VM" "${G[@]}" -q >&2
  t1=$(now)
  echo "suspended in $(since $t0 $t1)s; status $(status)" >&2
  sleep "$wait_s"
  t2=$(now)
  gcloud compute instances resume "$VM" "${G[@]}" -q >&2
  t3=$(now)
  until [ "$(status)" = RUNNING ]; do sleep 0.5; done
  t4=$(now)
  IP=$(ip)
  until get /health > /dev/null 2>&1; do sleep 0.2; done
  t5=$(now)
  first=$(get "/run?k=$FIRST")
  t6=$(now)
  all=$(get "/run")
  python3 - "$wait_s" "$t0" "$t1" "$t2" "$t3" "$t4" "$t5" "$t6" "$base" "$first" "$all" <<'PY' | tee -a $OUT/a-cycles.jsonl
import json, sys
w, t0, t1, t2, t3, t4, t5, t6 = sys.argv[1], *map(float, sys.argv[2:9])
base, first, allq = (json.loads(x) for x in sys.argv[9:12])
print(json.dumps({
    "suspended_s": int(w), "suspend_api_s": round(t1 - t0, 2), "resume_api_s": round(t3 - t2, 2), "to_running_s": round(t4 - t2, 2),
    "to_health_s": round(t5 - t2, 2), "to_first_answer_s": round(t6 - t2, 2),
    "first_before_ms": [r["ms"] for r in base["results"]], "first_after_ms": [r["ms"] for r in first["results"]],
    "first_after_verdicts": [r["verdict"] for r in first["results"]],
    "all_after": {"tally": allq["tally"], **{k: allq["latency"][k] for k in ("p50_ms", "p90_ms", "max_ms")}, "wall_ms": allq["wall_ms"]},
}))
PY
  ;;
stopstart)
  IP=$(ip)
  t0=$(now)
  gcloud compute instances stop "$VM" "${G[@]}" -q >&2
  t1=$(now)
  gcloud compute instances start "$VM" "${G[@]}" -q >&2
  t2=$(now)
  IP=$(ip)
  until vssh true 2>/dev/null; do sleep 0.5; done
  t3=$(now)
  until vssh test -f /data/.setup 2>/dev/null; do sleep 0.5; done
  vssh "until sudo docker info >/dev/null 2>&1; do sleep 0.2; done; $(serve_cmd /data/stage/mem-index "")" > /dev/null
  until get /health > $OUT/a-health-restart.json 2>/dev/null; do sleep 0.5; done
  t4=$(now)
  first=$(get "/run?k=$FIRST")
  t5=$(now)
  all=$(get "/run")
  python3 - "$t0" "$t1" "$t2" "$t3" "$t4" "$t5" "$first" "$all" "$(cat $OUT/a-health-restart.json)" <<'PY' | tee -a $OUT/a-stopstart.jsonl
import json, sys
t0, t1, t2, t3, t4, t5 = map(float, sys.argv[1:7])
first, allq, health = (json.loads(x) for x in sys.argv[7:10])
print(json.dumps({
    "stop_api_s": round(t1 - t0, 2), "start_api_s": round(t2 - t1, 2), "to_ssh_s": round(t3 - t1, 2), "to_health_s": round(t4 - t1, 2),
    "to_first_answer_s": round(t5 - t1, 2), "load": health["load"], "first_after_ms": [r["ms"] for r in first["results"]],
    "all_after": {"tally": allq["tally"], **{k: allq["latency"][k] for k in ("p50_ms", "p90_ms", "max_ms")}, "wall_ms": allq["wall_ms"]},
}))
PY
  ;;
delete)
  gcloud compute instances delete "$VM" "${G[@]}" -q --delete-disks=all
  gcloud compute instances list --filter="labels.purpose=serving-exp" --format='value(name)'
  gcloud compute disks list --filter="name~^$VM" --format='value(name)'
  ;;
esac
