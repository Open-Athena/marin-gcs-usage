#!/usr/bin/env bash
# Serving-box bake-off on GCP Batch (specs/filter-query-service.md §6 phase 2).
# Runs inside the gcs job image; `dt_cloud` comes from the staged source under
# $SB/src (uploaded by `job/serving-box-submit.sh`), outputs go to $SB/out/$BENCH_JOB.
#
#   serving-box-bench.sh build   # build the mem index from GEN, upload it, score it (warm from NVMe)
#   serving-box-bench.sh target  # on the target VM size: mem cold from GCS + warm from NVMe (+ mapped: STEPS=…,mmap;
#                                # + `serve-query` scored over HTTP: STEPS=…,serve),
#                                # DuckDB names-first, then both through gcsfuse / rclone mounts
set -uo pipefail
MODE=${1:?build|target|py}
JOB=${BENCH_JOB:-local}
B=oa-gcs-usage-dvx
SB=scratch/bench/serving-box
GEN=${GEN:-gs://$B/listing/2026-10-01/index/20261002T113031Z}
GEN_KEY=${GEN#gs://$B/}
INDEX=${INDEX:-gs://$B/$SB/index/2026-10-01}
INDEX_KEY=${INDEX#gs://$B/}
Q=gs://$B/scratch/bench/queries/gcs.yml
T=gs://$B/scratch/bench/truth/2026-10-01/
THREADS=${THREADS:-$(nproc)}
OUT=/stage/out
mkdir -p $OUT /stage/src /stage/duck_tmp
cp -r /gcs/$B/$SB/src/. /stage/src/
export PYTHONPATH=/stage/src
cd /stage/src
nproc; head -2 /proc/meminfo; df -h /stage
python3 -c 'import duckdb, pyarrow, numpy; print("duckdb", duckdb.__version__, "pyarrow", pyarrow.__version__, "numpy", numpy.__version__)'

dtc() { python3 -u -m dt_cloud.cli "$@"; }
runs=gs://$B/$SB/out/$JOB/runs/
step() { echo "=== $* ($(date -u +%T))"; }

upload_out() {
  python3 - <<PY
import os
from google.cloud import storage
b = storage.Client().bucket('$B')
for root, _, files in os.walk('$OUT'):
    for fn in files:
        p = os.path.join(root, fn)
        b.blob('$SB/out/$JOB/' + os.path.relpath(p, '$OUT')).upload_from_filename(p)
PY
}
trap upload_out EXIT

case $MODE in
py)  # a one-off script staged beside this one: serving-box-bench.sh py <script.py> [args…]
  python3 -u /gcs/$B/$SB/scripts/$2 "${@:3}" 2>&1 | tee $OUT/py.log
  ;;
build)
  step "bench-index"
  /usr/bin/time -v python3 -u -m dt_cloud.cli bench-index -s /stage/gen -t "$THREADS" -m "${DUCK_MEM:-180GB}" -T /stage/duck_tmp \
    -o /stage/mem-index -u "$INDEX" "$GEN" > $OUT/build.json 2> >(tee $OUT/build.log >&2)
  ls -la /stage/mem-index | tee $OUT/index-ls.txt
  rm -rf /stage/duck_tmp/*
  step "mem, warm from NVMe (page cache dropped), ${ENGINE_THREADS:-8} threads"
  dtc bench-engine -e mem -i /stage/mem-index -E -t "${ENGINE_THREADS:-8}" -n mem-nvme -Q $Q -T $T -o $runs "$GEN" 2>&1 | tee $OUT/mem-nvme.log
  ;;
target)
  STEPS=${STEPS:-mem,duckdb,mounts}
  if [[ ,$STEPS, == *,mem,* ]]; then
  step "mem, cold from GCS (parallel ranged GETs → NVMe → RAM)"
  dtc bench-engine -e mem -i "$INDEX/" -s /stage/cold -t "$THREADS" -n mem-gcs -Q $Q -T $T -o $runs "$GEN" 2>&1 | tee $OUT/mem-gcs.log
  step "mem, warm from NVMe (page cache dropped)"
  dtc bench-engine -e mem -i /stage/cold/mem-index -E -t "$THREADS" -n mem-nvme -k tomat -k step -Q $Q -T $T -o $runs "$GEN" 2>&1 | tee $OUT/mem-nvme.log
  if [[ ,$STEPS, == *,mmap,* ]]; then
  step "mem, mapped from NVMe (page cache dropped): every query"
  dtc bench-engine -e mem -M -i /stage/cold/mem-index -E -t "$THREADS" -n mem-mmap -Q $Q -T $T -o $runs "$GEN" 2>&1 | tee $OUT/mem-mmap.log
  fi
  if [[ ,$STEPS, == *,serve,* ]]; then
  step "serve-query over the staged index, scored through HTTP (probe -Q: full responses)"
  mkdir -p /stage/root && ln -sfn /stage/cold/mem-index "/stage/root/${SCAN:-2026-10-01}"
  QUERY_BOX_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
  export QUERY_BOX_TOKEN
  python3 -u -m dt_cloud.cli serve-query -p 8080 -t "$THREADS" /stage/root > $OUT/serve.log 2>&1 &
  spid=$!
  for _ in $(seq 120); do curl -sf localhost:8080/healthz > /dev/null && break; sleep 1; done
  t0=$(date +%s.%N)
  curl -sf -H "Authorization: Bearer $QUERY_BOX_TOKEN" localhost:8080/warm | tee $OUT/warm.json; echo
  echo "warm_s $(python3 -c "print(round($(date +%s.%N) - $t0, 2))")" | tee -a $OUT/serve-mem.txt
  grep -E 'VmRSS|VmHWM' /proc/$spid/status | tee -a $OUT/serve-mem.txt
  GCS_USAGE_TOKEN=$QUERY_BOX_TOKEN dtc probe -Q $Q -T $T -u http://127.0.0.1:8080 ${PROBE_ARGS:-} -o $runs 2>&1 | tee $OUT/probe-box.log
  grep -E 'VmRSS|VmHWM' /proc/$spid/status | tee -a $OUT/serve-mem.txt
  kill $spid
  fi
  rm -rf /stage/cold
  fi
  if [[ ,$STEPS, == *,duckdb,* ]]; then
  step "duckdb names-first, generation copied to NVMe"
  dtc bench-engine -e duckdb -s /stage/gen -t "$THREADS" -m "${DUCK_MEM:-44GB}" -d /stage/duck_tmp -n duckdb-nvme -Q $Q -T $T -o $runs "$GEN" 2>&1 | tee $OUT/duckdb-nvme.log
  rm -rf /stage/gen /stage/duck_tmp/*
  fi
  [[ ,$STEPS, == *,mounts,* ]] || exit 0

  step "install gcsfuse + rclone"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq --no-install-recommends fuse3 unzip > /dev/null
  curl -fsSL -o /tmp/gcsfuse.deb https://github.com/GoogleCloudPlatform/gcsfuse/releases/download/v3.12.1/gcsfuse_3.12.1_amd64.deb && dpkg -i /tmp/gcsfuse.deb > /dev/null
  curl -fsSL -o /tmp/rclone.zip https://downloads.rclone.org/v1.75.1/rclone-v1.75.1-linux-amd64.zip && unzip -q -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin
  gcsfuse --version; rclone version | head -1
  SUBSET=(-k tomat -k grug -k safetensors -k ckpt-not-eval -k tomat-end -k re-tokenizer-json -k podcast)
  mnt=/mnt/sb
  # allocated bytes (what the NVMe holds; a sparse rclone cache file holds less than its size), then apparent
  footprint() { echo "allocated $(du -s --block-size=1 "$1" | cut -f1) apparent $(du -s --apparent-size --block-size=1 "$1" | cut -f1)"; }

  for layer in ${LAYERS:-gcsfuse rclone}; do
    cache=/stage/cache-$layer
    rm -rf "$cache"; mkdir -p "$cache" $mnt
    step "$layer: mount"
    t0=$(date +%s)
    if [ $layer = gcsfuse ]; then
      gcsfuse --implicit-dirs --cache-dir "$cache" --file-cache-max-size-mb -1 --file-cache-cache-file-for-range-read \
        --file-cache-enable-parallel-downloads --stat-cache-max-size-mb 64 --type-cache-max-size-mb 16 $B $mnt
    else
      rclone mount ":gcs,env_auth=true,bucket_policy_only=true:$B" $mnt --daemon --read-only --vfs-cache-mode full \
        --cache-dir "$cache" --vfs-cache-max-size 300G --vfs-read-chunk-size 64M --vfs-read-chunk-streams 16 --buffer-size 0 --dir-cache-time 1h
      for _ in $(seq 60); do [ -d "$mnt/$GEN_KEY" ] && break; sleep 1; done
    fi
    echo "mount_s $(( $(date +%s) - t0 ))" | tee -a $OUT/$layer.log
    if [ "${MOUNT_MEM:-1}" = 1 ]; then
    step "$layer: mem index through the mount (time to a hot generation)"
    dtc bench-engine -e mem -i "$mnt/$INDEX_KEY" -t "$THREADS" -n mem-$layer -k tomat -k step -Q $Q -T $T -o $runs "$GEN" 2>&1 | tee -a $OUT/$layer.log
    echo "footprint after mem load: $(footprint "$cache")" | tee -a $OUT/$layer.log
    fi
    step "$layer: duckdb over the mount, cold then warm"
    dtc bench-engine -e duckdb -t "$THREADS" -m "${DUCK_MEM:-44GB}" -d /stage/duck_tmp -n duckdb-$layer-cold "${SUBSET[@]}" -Q $Q -T $T -o $runs "$mnt/$GEN_KEY" 2>&1 | tee -a $OUT/$layer.log
    echo "footprint after duckdb cold: $(footprint "$cache")" | tee -a $OUT/$layer.log
    dtc bench-engine -e duckdb -t "$THREADS" -m "${DUCK_MEM:-44GB}" -d /stage/duck_tmp -n duckdb-$layer-warm "${SUBSET[@]}" -Q $Q -T $T -o $runs "$mnt/$GEN_KEY" 2>&1 | tee -a $OUT/$layer.log
    echo "footprint after duckdb warm: $(footprint "$cache")" | tee -a $OUT/$layer.log
    ls -laR "$cache" > $OUT/$layer-cache-ls.txt 2>&1
    fusermount3 -u $mnt || umount $mnt
    rm -rf "$cache"
  done
  ;;
esac
