#!/usr/bin/env bash
# The ClickHouse store (specs/ch-store.md) on a short-lived VM in us-east1,
# beside gcs's data: ClickHouse + `dt-cloud serve-query -e ch` in the gcs job
# image (this checkout's sources on PYTHONPATH). Run from a laptop.
#
#   job/ch-store.sh create               # VM: ClickHouse (stable), docker + the job image, sources, a box token
#   job/ch-store.sh push                 # this checkout's dt_cloud / disk_tree + job/ch-store/* → the VM
#   job/ch-store.sh push-src             # Python sources only; do not re-upload/re-copy unchanged node scripts
#   job/ch-store.sh ingest DATE…         # background: ch-ingest each scan in order (job/ch-store/ingest-days.sh)
#   job/ch-store.sh serve                # (re)start serve-query -e ch on :8080 (bearer token in /data/token)
#   job/ch-store.sh py ARGS…             # `python3 -m dt_cloud.cli ARGS` in the image on the VM (privileged, host network)
#   job/ch-store.sh py-bg TAG ARGS…      # detached CLI job; refuses an existing tag, retains exit state/logs
#   job/ch-store.sh py-status TAG        # read-only Docker job state (JSON); no environment/token output
#   job/ch-store.sh py-logs TAG          # read-only retained job output (capture to local tmp/)
#   job/ch-store.sh sh CMD               # a command on the VM
#   job/ch-store.sh sql FILE|-           # SQL on the VM (clickhouse-client --time, multiquery)
#   job/ch-store.sh test [PYTEST_ARGS…]  # sync small fixtures/tests and run beside ClickHouse (no SSH round-trip per query)
#   job/ch-store.sh delete               # VM + disk, then verify they're gone
#
# Env: VM (ch-store), ZONE (us-east1-c), MACHINE (n2-custom-8-16384), DISK_TYPE (pd-ssd), DISK_GB (500).
# serve: HOT_L1_GENERATION pins a published scan-free catalog alongside normal routes.
# HOT_L2_ARTIFACT and HOT_L2_CHECK together select an accepted bucket-drill artifact.
# NAME_SUMMARY=1 opts into bounded stitched root summaries with HOT_L1_GENERATION and NARROW_TARGET.
# DATED_L1_GENERATION and DATED_NAME_STORE together add accepted daily catalogs; require NAME_SUMMARY=1.
# DATED_COLD=1 answers their unregistered literals over each scan's own name index (`ch-daily-name-index`).
# NARROW_TARGET opts into numeric history; NARROW_{RICH_NAME,DIRECTORY_PARENT}_INDEX=1 select completed indexes.
# NARROW_RICH_NAME_VARIANT selects a completed rich-name variant and requires NARROW_RICH_NAME_INDEX=1.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
VM=${VM:-ch-store}
ZONE=${ZONE:-us-east1-c}
MACHINE=${MACHINE:-n2-custom-8-16384}
DISK_TYPE=${DISK_TYPE:-pd-ssd}
DISK_GB=${DISK_GB:-500}
B=oa-gcs-usage-dvx
X=gs://$B/scratch/bench/ch-store
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
G=(--project "$PROJECT" --zone "$ZONE")

ip() { gcloud compute instances describe "$VM" "${G[@]}" --format='value(networkInterfaces[0].accessConfigs[0].natIP)'; }
vssh() { ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=30 -i ~/.ssh/google_compute_engine "$USER@$IP" "$@"; }
job_name() {
  if [[ ! ${1:-} =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
    printf '%s\n' 'job tag must be 1..63 lowercase letters, digits, underscores or hyphens, starting with a letter or digit' >&2
    exit 2
  fi
  printf 'ch-job-%s' "$1"
}
stage_sources() {
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$X/src/dt_cloud" >&2
  gcloud storage rsync -r -x '.*__pycache__.*' src/disk_tree "$X/src/disk_tree" >&2
}
stage() {
  stage_sources
  local -a CH_SCRIPTS=()
  local ch_script
  for ch_script in job/ch-store/*; do
    if [ -f "$ch_script" ]; then CH_SCRIPTS+=("$ch_script"); fi
  done
  gcloud storage cp "${CH_SCRIPTS[@]}" "$X/scripts/" >&2
}

case ${1:?create|push|push-src|ingest|serve|py|py-bg|py-status|py-logs|sh|sql|delete} in
create)
  stage
  startup=$(mktemp)
  cat > "$startup" <<EOF
#!/bin/bash
[ -f /data/.setup ] && exit 0
mkdir -p /data/in /data/src
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq apt-transport-https ca-certificates curl gnupg docker.io > /dev/null
curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=amd64] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list
apt-get update -qq && apt-get install -y -qq clickhouse-server clickhouse-client > /dev/null
mkdir -p /etc/clickhouse-server/config.d
cat > /etc/clickhouse-server/config.d/store.xml <<'XML'
<clickhouse><user_files_path>/data/in/</user_files_path></clickhouse>
XML
chmod 777 /data/in
systemctl enable --now clickhouse-server
gcloud auth configure-docker us-central1-docker.pkg.dev -q
docker pull -q $IMAGE
echo $IMAGE > /data/image
openssl rand -hex 24 > /data/token && chmod 600 /data/token
gcloud storage cp -r $X/src/dt_cloud $X/src/disk_tree /data/src/
gcloud storage cp '$X/scripts/*' /data/ && chmod +x /data/*.sh
touch /data/.setup
EOF
  gcloud compute instances create "$VM" "${G[@]}" --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud --boot-disk-type "$DISK_TYPE" --boot-disk-size "${DISK_GB}GB" \
    --service-account "gcs-usage-job@$PROJECT.iam.gserviceaccount.com" --scopes cloud-platform \
    --labels purpose=ch-store --metadata-from-file startup-script="$startup"
  rm -f "$startup"
  ;;
push|push-src)
  PUSH_MODE=$1
  if [ "$PUSH_MODE" = push-src ]; then stage_sources; else stage; fi
  IP=$(ip)
  PUSH_SYNC="sudo rm -rf /data/src/dt_cloud /data/src/disk_tree && sudo gcloud storage cp -r '$X/src/dt_cloud' '$X/src/disk_tree' /data/src/ > /dev/null 2>&1"
  if [ "$PUSH_MODE" = push ]; then
    PUSH_SYNC+=" && sudo gcloud storage cp '$X/scripts/*' /data/ > /dev/null 2>&1 && sudo chmod +x /data/*.sh"
  fi
  vssh "$PUSH_SYNC && echo pushed"
  ;;
ingest)
  IP=$(ip)
  shift
  vssh "sudo nohup bash -c 'THREADS=${THREADS:-8} CH_INGEST_PAIRS=${CH_INGEST_PAIRS:-} /data/ingest-days.sh $*' > /dev/null 2>&1 < /dev/null & echo started"
  ;;
serve)
  case ${NAME_SUMMARY:-0} in
    0) ;;
    1)
      if [ -z "${HOT_L1_GENERATION:-}" ] || [ -z "${NARROW_TARGET:-}" ]; then
        printf '%s\n' 'NAME_SUMMARY requires HOT_L1_GENERATION and NARROW_TARGET' >&2
        exit 2
      fi
      ;;
    *) printf '%s\n' 'NAME_SUMMARY must be 0 or 1' >&2; exit 2 ;;
  esac
  if [ -n "${DATED_L1_GENERATION:-}" ] || [ -n "${DATED_NAME_STORE:-}" ]; then
    if [ -z "${DATED_L1_GENERATION:-}" ] || [ -z "${DATED_NAME_STORE:-}" ]; then
      printf '%s\n' 'DATED_L1_GENERATION and DATED_NAME_STORE are required together' >&2
      exit 2
    fi
    if [ "${NAME_SUMMARY:-0}" != 1 ]; then
      printf '%s\n' 'DATED_L1_GENERATION and DATED_NAME_STORE require NAME_SUMMARY=1' >&2
      exit 2
    fi
    if [[ ! $DATED_NAME_STORE =~ ^[a-z][a-z0-9_]*$ ]]; then
      printf '%s\n' 'DATED_NAME_STORE must match [a-z][a-z0-9_]*' >&2
      exit 2
    fi
  fi
  case ${DATED_COLD:-0} in
    0) ;;
    1) if [ -z "${DATED_L1_GENERATION:-}" ]; then printf '%s\n' 'DATED_COLD requires DATED_L1_GENERATION' >&2; exit 2; fi ;;
    *) printf '%s\n' 'DATED_COLD must be 0 or 1' >&2; exit 2 ;;
  esac
  for SERVE_INDEX in NARROW_RICH_NAME_INDEX NARROW_DIRECTORY_PARENT_INDEX; do
    case ${!SERVE_INDEX:-0} in
      0) ;;
      1) if [ -z "${NARROW_TARGET:-}" ]; then printf '%s requires NARROW_TARGET\n' "$SERVE_INDEX" >&2; exit 2; fi ;;
      *) printf '%s must be 0 or 1\n' "$SERVE_INDEX" >&2; exit 2 ;;
    esac
  done
  if [ -n "${NARROW_RICH_NAME_VARIANT:-}" ]; then
    if [[ ! $NARROW_RICH_NAME_VARIANT =~ ^[a-z][a-z0-9_]*$ ]]; then
      printf '%s\n' 'NARROW_RICH_NAME_VARIANT must match [a-z][a-z0-9_]*' >&2
      exit 2
    fi
    if [ "${NARROW_RICH_NAME_INDEX:-0}" != 1 ]; then
      printf '%s\n' 'NARROW_RICH_NAME_VARIANT requires NARROW_RICH_NAME_INDEX=1' >&2
      exit 2
    fi
  fi
  IP=$(ip)
  NARROW_ARGS=()
  CATALOG_ARGS=()
  if [ -n "${HOT_L1_GENERATION:-}" ]; then CATALOG_ARGS+=(-g "$HOT_L1_GENERATION"); fi
  if [ -n "${HOT_L2_ARTIFACT:-}" ] || [ -n "${HOT_L2_CHECK:-}" ]; then
    if [ -z "${HOT_L2_ARTIFACT:-}" ] || [ -z "${HOT_L2_CHECK:-}" ]; then
      printf '%s\n' 'HOT_L2_ARTIFACT and HOT_L2_CHECK are required together' >&2
      exit 2
    fi
    CATALOG_ARGS+=(-H "$HOT_L2_ARTIFACT" -J "$HOT_L2_CHECK")
  fi
  if [ "${NAME_SUMMARY:-0}" = 1 ]; then CATALOG_ARGS+=(-L); fi
  if [ -n "${DATED_L1_GENERATION:-}" ]; then CATALOG_ARGS+=(-G "$DATED_L1_GENERATION" -f "$DATED_NAME_STORE"); fi
  if [ "${DATED_COLD:-0}" = 1 ]; then CATALOG_ARGS+=(-C); fi
  if [ -n "${NARROW_TARGET:-}" ]; then NARROW_ARGS+=(-N "$NARROW_TARGET"); fi
  if [ "${NARROW_RICH_NAME_INDEX:-0}" = 1 ]; then NARROW_ARGS+=(-i); fi
  if [ -n "${NARROW_RICH_NAME_VARIANT:-}" ]; then NARROW_ARGS+=(-v "$NARROW_RICH_NAME_VARIANT"); fi
  if [ "${NARROW_DIRECTORY_PARENT_INDEX:-0}" = 1 ]; then NARROW_ARGS+=(-j); fi
  case ${NARROW_PLAN:-legacy} in
    legacy) ;;
    visible)
      if [ -z "${NARROW_TARGET:-}" ]; then printf '%s\n' 'NARROW_PLAN requires NARROW_TARGET' >&2; exit 2; fi
      NARROW_ARGS+=(-k visible)
      ;;
    *) printf '%s\n' 'NARROW_PLAN must be legacy or visible' >&2; exit 2 ;;
  esac
  SERVE_CMD="docker run -d --name serve-query --restart unless-stopped --network host -v /data:/data -e PYTHONPATH=/data/src \
    -e QUERY_BOX_TOKEN=\$(cat /data/token) --entrypoint python3 \$(cat /data/image) -u -m dt_cloud.cli \
    $(printf '%q ' serve-query -e ch -p 8080 -c "${CONCURRENCY:-2}" -t "${THREADS:-8}" -r "${ROOT_PLAN:-rich}" "${CATALOG_ARGS[@]}" "${NARROW_ARGS[@]}" http://localhost:8123)"
  vssh "sudo docker rm -f serve-query > /dev/null 2>&1; sudo bash -c $(printf '%q' "$SERVE_CMD")"
  ;;
py|py-bg)
  PY_MODE=$1
  shift
  PY_RUN=(run --rm)
  if [ "$PY_MODE" = py-bg ]; then
    PY_NAME=$(job_name "${1:-}")
    shift
    if [ "$#" -eq 0 ]; then printf '%s\n' 'py-bg requires a CLI command' >&2; exit 2; fi
    PY_RUN=(run -d --name "$PY_NAME" --log-opt max-size=10m --log-opt max-file=2)
  fi
  IP=$(ip)
  PY_CMD="docker $(printf '%q ' "${PY_RUN[@]}") --privileged --network host -v /data:/data -v /proc:/hostproc -e PYTHONPATH=/data/src -e QUERY_BOX_TOKEN=\$(cat /data/token) \
    --entrypoint python3 \$(cat /data/image) -u -m dt_cloud.cli $(printf '%q ' "$@")"
  vssh "sudo bash -c $(printf '%q' "$PY_CMD")"
  ;;
py-status|py-logs)
  PY_MODE=$1
  PY_NAME=$(job_name "${2:-}")
  IP=$(ip)
  if [ "$PY_MODE" = py-status ]; then
    vssh "sudo docker inspect --format '{{json .State}}' $PY_NAME"
  else
    vssh "sudo docker logs $PY_NAME"
  fi
  ;;
sh)
  IP=$(ip)
  vssh "$2"
  ;;
sql)
  IP=$(ip)
  if [ "$2" = - ]; then cat; else cat "$2"; fi | vssh "clickhouse-client --time --multiquery --progress=off ${CH_ARGS:-}"
  ;;
test)
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/tests "$X/test-checkout/cloud/tests" >&2
  gcloud storage rsync -r site/functions/_lib/fixtures "$X/test-checkout/site/functions/_lib/fixtures" >&2
  gcloud storage cp job/ch-store/tests.sh "$X/scripts/tests.sh" >&2
  IP=$(ip)
  shift
  vssh "sudo mkdir -p /data/test-checkout && sudo gcloud storage rsync -r '$X/test-checkout' /data/test-checkout > /dev/null 2>&1 && \
    sudo gcloud storage cp '$X/scripts/tests.sh' /data/tests.sh > /dev/null 2>&1 && sudo env $(printf '%q ' "HL1_NATIVE_BINARY=${HL1_NATIVE_BINARY:-}" "HF_NATIVE_BINARY=${HF_NATIVE_BINARY:-}" "HL2_NATIVE_BINARY=${HL2_NATIVE_BINARY:-}" "HL2_NATIVE_SOURCE_BINARY=${HL2_NATIVE_SOURCE_BINARY:-}") bash /data/tests.sh $(printf '%q ' "$@")"
  ;;
delete)
  gcloud compute instances delete "$VM" "${G[@]}" -q --delete-disks=all
  gcloud compute instances list --filter="labels.purpose=ch-store" --format='value(name)'
  gcloud compute disks list --filter="name~^$VM" --format='value(name)'
  ;;
*)
  printf 'unknown ch-store command: %s\n' "$1" >&2
  exit 2
  ;;
esac
