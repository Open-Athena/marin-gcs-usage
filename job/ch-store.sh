#!/usr/bin/env bash
# The ClickHouse store (specs/ch-store.md) on a short-lived VM in us-east1,
# beside gcs's data: ClickHouse + `dt-cloud serve-query -e ch` in the gcs job
# image (this checkout's sources on PYTHONPATH). Run from a laptop.
#
#   job/ch-store.sh create               # VM: ClickHouse (stable), docker + the job image, sources, a box token
#   job/ch-store.sh push                 # this checkout's dt_cloud / disk_tree + job/ch-store/* → the VM
#   job/ch-store.sh ingest DATE…         # background: ch-ingest each scan in order (job/ch-store/ingest-days.sh)
#   job/ch-store.sh serve                # (re)start serve-query -e ch on :8080 (bearer token in /data/token)
#   job/ch-store.sh py ARGS…             # `python3 -m dt_cloud.cli ARGS` in the image on the VM (privileged, host network)
#   job/ch-store.sh sh CMD               # a command on the VM
#   job/ch-store.sh sql FILE|-           # SQL on the VM (clickhouse-client --time, multiquery)
#   job/ch-store.sh delete               # VM + disk, then verify they're gone
#
# Env: VM (ch-store), ZONE (us-east1-c), MACHINE (n2-custom-8-16384), DISK_TYPE (pd-ssd), DISK_GB (500).
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
stage() {
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$X/src/dt_cloud" >&2
  gcloud storage rsync -r -x '.*__pycache__.*' src/disk_tree "$X/src/disk_tree" >&2
  gcloud storage cp job/ch-store/* "$X/scripts/" >&2
}

case ${1:?create|push|ingest|serve|py|sh|sql|delete} in
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
push)
  stage
  IP=$(ip)
  vssh "sudo rm -rf /data/src/dt_cloud /data/src/disk_tree && sudo gcloud storage cp -r '$X/src/dt_cloud' '$X/src/disk_tree' /data/src/ > /dev/null 2>&1 && \
    sudo gcloud storage cp '$X/scripts/*' /data/ > /dev/null 2>&1 && sudo chmod +x /data/*.sh && echo pushed"
  ;;
ingest)
  IP=$(ip)
  shift
  vssh "sudo nohup bash -c 'THREADS=${THREADS:-8} CH_INGEST_PAIRS=${CH_INGEST_PAIRS:-} /data/ingest-days.sh $*' > /dev/null 2>&1 < /dev/null & echo started"
  ;;
serve)
  IP=$(ip)
  vssh "sudo docker rm -f serve-query > /dev/null 2>&1; sudo bash -c 'docker run -d --name serve-query --network host -v /data:/data -e PYTHONPATH=/data/src \
    -e QUERY_BOX_TOKEN=\$(cat /data/token) --entrypoint python3 \$(cat /data/image) -u -m dt_cloud.cli serve-query -e ch -p 8080 -t ${THREADS:-8} http://localhost:8123'"
  ;;
py)
  IP=$(ip)
  shift
  vssh "sudo bash -c 'docker run --rm --privileged --network host -v /data:/data -v /proc:/hostproc -e PYTHONPATH=/data/src -e QUERY_BOX_TOKEN=\$(cat /data/token) \
    --entrypoint python3 \$(cat /data/image) -u -m dt_cloud.cli $(printf '%q ' "$@")'"
  ;;
sh)
  IP=$(ip)
  vssh "$2"
  ;;
sql)
  IP=$(ip)
  if [ "$2" = - ]; then cat; else cat "$2"; fi | vssh "clickhouse-client --time --multiquery --progress=off ${CH_ARGS:-}"
  ;;
delete)
  gcloud compute instances delete "$VM" "${G[@]}" -q --delete-disks=all
  gcloud compute instances list --filter="labels.purpose=ch-store" --format='value(name)'
  gcloud compute disks list --filter="name~^$VM" --format='value(name)'
  ;;
esac
