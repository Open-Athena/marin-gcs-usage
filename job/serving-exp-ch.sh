#!/usr/bin/env bash
# Experiment B (specs/serving-options.md): self-hosted ClickHouse on a modest
# disk-backed VM in us-east1, holding gcs's latest scan (the filter bench's
# tables, `dt_cloud.bench.ch`) and an SCD-2 history of daily scans (objects
# from the per-object listings, dir rollups from `dir-cache/dir-stats.parquet`).
# Run from a laptop; SQL lives in job/serving-exp-ch/*.sql.
#
#   job/serving-exp-ch.sh create             # VM: ClickHouse (stable), docker + the gcs job image, src
#   job/serving-exp-ch.sh sh CMD             # a command on the VM
#   job/serving-exp-ch.sh sql FILE|-         # run SQL on the VM (clickhouse-client --time, multiquery)
#   job/serving-exp-ch.sh delete             # VM + disk, then verify they're gone
#
# Env: VM (serving-exp-ch), ZONE (us-east1-c), MACHINE (n2-standard-8), DISK_TYPE (pd-ssd), DISK_GB (500).
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT=oa-internal-450019
VM=${VM:-serving-exp-ch}
ZONE=${ZONE:-us-east1-c}
MACHINE=${MACHINE:-n2-standard-8}
DISK_TYPE=${DISK_TYPE:-pd-ssd}
DISK_GB=${DISK_GB:-500}
B=oa-gcs-usage-dvx
X=gs://$B/scratch/bench/serving-exp
IMAGE=${IMAGE:-us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/gcs-usage-snapshot:latest}
G=(--project "$PROJECT" --zone "$ZONE")

ip() { gcloud compute instances describe "$VM" "${G[@]}" --format='value(networkInterfaces[0].accessConfigs[0].natIP)'; }
vssh() { ssh -q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o BatchMode=yes -o ServerAliveInterval=30 -i ~/.ssh/google_compute_engine "$USER@$IP" "$@"; }

case ${1:?create|sh|sql|delete} in
create)
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$X/src/dt_cloud" >&2
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
# user_files = /data/in (load with file()); no password on localhost
mkdir -p /etc/clickhouse-server/config.d
cat > /etc/clickhouse-server/config.d/exp.xml <<'XML'
<clickhouse><user_files_path>/data/in/</user_files_path></clickhouse>
XML
chown -R clickhouse:clickhouse /data/in
chmod 777 /data/in
systemctl enable --now clickhouse-server
gcloud auth configure-docker us-central1-docker.pkg.dev -q
docker pull -q $IMAGE
gcloud storage cp -r $X/src/dt_cloud /data/src/
touch /data/.setup
EOF
  gcloud compute instances create "$VM" "${G[@]}" --machine-type "$MACHINE" \
    --image-family debian-12 --image-project debian-cloud --boot-disk-type "$DISK_TYPE" --boot-disk-size "${DISK_GB}GB" \
    --service-account "gcs-usage-job@$PROJECT.iam.gserviceaccount.com" --scopes cloud-platform \
    --labels purpose=serving-exp --metadata-from-file startup-script="$startup"
  rm -f "$startup"
  ;;
sh)
  IP=$(ip)
  vssh "$2"
  ;;
push)  # job/serving-exp-ch/* → the VM's /data/
  IP=$(ip)
  gcloud storage cp job/serving-exp-ch/* "$X/scripts/" >&2
  vssh "sudo gcloud storage cp '$X/scripts/*' /data/ > /dev/null 2>&1; sudo chmod +x /data/*.sh"
  ;;
bg)  # run /data/<file> (.sql via clickhouse-client, else as a script) in the background → /data/<file>.log
  IP=$(ip)
  f=$2; shift 2
  if [[ $f == *.sql ]]; then run="clickhouse-client --time --multiquery --progress=off $* < /data/$f"; elif [[ $f == *.py ]]; then run="sudo python3 /data/$f $*"; else run="sudo /data/$f $*"; fi
  vssh "nohup bash -c '$run' > /data/$f.log 2>&1 < /dev/null & echo started"
  ;;
bench)  # dt-cloud bench-engine -e ch (this checkout's src) in the gcs job image on the VM; extra args forwarded
  IP=$(ip)
  shift
  gcloud storage rsync -r -x '.*__pycache__.*' cloud/src/dt_cloud "$X/src/dt_cloud" >&2
  vssh "sudo rm -rf /data/src/dt_cloud && sudo gcloud storage cp -r '$X/src/dt_cloud' /data/src/ > /dev/null 2>&1 && \
    sudo docker run --rm --privileged --network host -v /data:/data -e PYTHONPATH=/data/src --entrypoint python3 $IMAGE -u -m dt_cloud.cli \
    bench-engine -e ch -t 8 -Q gs://$B/scratch/bench/queries/gcs.yml -T gs://$B/scratch/bench/truth/2026-10-01/ $(printf '%q ' "$@") \
    gs://$B/listing/2026-10-01/index/20261002T113031Z"
  ;;
sql)
  IP=$(ip)
  if [ "$2" = - ]; then cat; else cat "$2"; fi | vssh "clickhouse-client --time --multiquery --progress=off ${CH_ARGS:-}"
  ;;
delete)
  gcloud compute instances delete "$VM" "${G[@]}" -q --delete-disks=all
  gcloud compute instances list --filter="labels.purpose=serving-exp" --format='value(name)'
  gcloud compute disks list --filter="name~^$VM" --format='value(name)'
  ;;
esac
