#!/usr/bin/env bash
# Path-store phase 0 (specs/path-store.md §5, phase 0): measure the store's two
# sorts on the daily job's machine class, over one cw layer-2 — the production
# cost of the phase-1 writer, not a laptop's. Runs inside the `:cw` image as one
# GCP Batch task (job/cw-path-store-phase0-submit.sh). Writes nothing under
# `cw-l2/` or `snapshots/`; everything lands under OUT_URI (a scratch prefix).
#
#   1. stage the layer-2 on local disk (`L2_URI`; a 2 GiB zstd v2 listing)
#   2. `disk-tree tiers cut -g` under the image's codec (zstd): both sorts +
#      their `.groups.json` sidecars — wall, peak RSS, rows/groups/bytes per sort
#   3. the same cut under `DISK_TREE_PARQUET_CODEC=snappy` (bytes only)
#   4. pick four read targets from the layer-2 itself: the root, the dir nearest
#      10 TiB, the dir nearest 100 GiB, and the flattest dir (most children)
#   5. `disk-tree tiers plan -a 2` for each target × each sort at
#      thr = size(P) · 12 / (1280 · 800) — the reader's `minArea` / viewport rule
#      — reporting groups selected, rows decoded, bytes, matched rows, waste
#   6. upload the tiers, sidecars and reports to OUT_URI, print the summary
#
# Env: L2_URI, OUT_URI, WORK_DIR (default /stage/phase0).
set -euxo pipefail
L2_URI=${L2_URI:-gs://oa-gcs-usage-dvx/cw-l2/2026-09-30T1203/marin-us-east-02a.parquet}
OUT_URI=${OUT_URI:-gs://oa-gcs-usage-dvx/path-store-phase0/2026-09-30T1203}
WORK=${WORK_DIR:-/stage/phase0}
mkdir -p "$WORK/store" "$WORK/report"
cd /app
ulimit -n "$(ulimit -Hn)" 2>/dev/null || ulimit -n 65536

# 1. stage
python3 - "$L2_URI" "$WORK/l2.parquet" <<'PY'
import sys; from google.cloud import storage
uri, dst = sys.argv[1:]
b, key = uri[len('gs://'):].split('/', 1)
blob = storage.Client().bucket(b).blob(key); blob.reload()
print(f'  ↓ {uri} {blob.size:,} B', file=sys.stderr)
blob.download_to_filename(dst)
PY
ls -l "$WORK/l2.parquet"
disk-tree listing-format "$WORK/l2.parquet"

# `timed OUT CMD…`: run CMD, record wall / peak RSS / cpu of the child and its
# JSON stdout in OUT (a fresh interpreter per call, so RUSAGE_CHILDREN is CMD's).
timed() {
  python3 - "$@" <<'PY'
import json, resource, subprocess, sys, time
out, cmd = sys.argv[1], sys.argv[2:]
t0 = time.monotonic()
p = subprocess.run(cmd, stdout=subprocess.PIPE)
wall = time.monotonic() - t0
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
so = p.stdout.decode()
rep = {
    'cmd': cmd, 'rc': p.returncode, 'wall_s': round(wall, 1), 'peak_rss_mib': round(ru.ru_maxrss / 1024),
    'user_s': round(ru.ru_utime, 1), 'sys_s': round(ru.ru_stime, 1),
    'stdout': json.loads(so) if so.lstrip().startswith(('{', '[')) else so,
}
json.dump(rep, open(out, 'w'), indent=2)
print('timed:', json.dumps({k: v for k, v in rep.items() if k != 'stdout'}))
sys.exit(p.returncode)
PY
}

# 2 + 3. the cut, zstd (the image's default) with sidecars, then snappy for the bytes
timed "$WORK/report/cut-zstd.json" disk-tree tiers cut -g -j -s "$WORK/store/zstd" "$WORK/l2.parquet"
DISK_TREE_PARQUET_CODEC=snappy timed "$WORK/report/cut-snappy.json" disk-tree tiers cut -j -s "$WORK/store/snappy" "$WORK/l2.parquet"
ls -l "$WORK/store" | tee "$WORK/report/sizes.txt"

# 4. read targets, from the layer-2 (dir rows; the root is `.`)
python3 - "$WORK/l2.parquet" "$WORK/report/targets.json" <<'PY'
import duckdb, json, sys
l2, out = sys.argv[1:]
con = duckdb.connect()
q = lambda sql: con.execute(sql).fetchall()
TiB, GiB = 2 ** 40, 2 ** 30
t = {'root': q(f"SELECT path, size, n_children FROM read_parquet('{l2}') WHERE path = '.'")[0]}
for name, target in [('10TiB', 10 * TiB), ('100GiB', 100 * GiB)]:
    t[name] = q(
        f"SELECT path, size, n_children FROM read_parquet('{l2}') WHERE kind = 'dir' AND path <> '.' AND size > 0 "
        f"ORDER BY abs(ln(size::DOUBLE) - ln({target}::DOUBLE)) LIMIT 1"
    )[0]
t['flat'] = q(f"SELECT path, size, n_children FROM read_parquet('{l2}') WHERE kind = 'dir' ORDER BY n_children DESC, path LIMIT 1")[0]
targets = {k: {'path': p, 'size': s, 'n_children': n} for k, (p, s, n) in t.items()}
json.dump(targets, open(out, 'w'), indent=2)
print(json.dumps(targets, indent=2))
PY

# 5. the reader's span selection, offline, per target × sort
python3 - "$WORK" <<'PY'
import glob, json, os, subprocess, sys
w = sys.argv[1]
targets = json.load(open(f'{w}/report/targets.json'))
rows = []
for sc in sorted(glob.glob(f'{w}/store/zstd.*.groups.json')):
    tier = os.path.basename(sc)[len('zstd.'):-len('.groups.json')]
    for name, t in targets.items():
        thr = max(1, int(t['size'] * 12 / (1280 * 800)))
        out = f'{w}/report/plan-{name}-{tier}.json'
        with open(out, 'w') as f:
            rc = subprocess.run(['disk-tree', 'tiers', 'plan', '-j', '-a', '2', sc, t['path'], str(thr)], stdout=f).returncode
        rep = json.load(open(out)) if rc == 0 else {'error': rc}
        rows.append({'target': name, 'tier': tier, **{k: v for k, v in rep.items() if k != 'selected'}})
json.dump(rows, open(f'{w}/report/plans.json', 'w'), indent=2)
for r in rows:
    print('plan:', json.dumps(r))
PY

# 6. publish
python3 - "$WORK" "$OUT_URI" <<'PY'
import os, sys; from google.cloud import storage
w, uri = sys.argv[1:]
b, prefix = uri[len('gs://'):].split('/', 1)
bk = storage.Client().bucket(b)
for sub in ('store', 'report'):
    for n in sorted(os.listdir(f'{w}/{sub}')):
        p = f'{w}/{sub}/{n}'
        bk.blob(f'{prefix}/{sub}/{n}').upload_from_filename(p, checksum='md5')
        print(f'  ↑ gs://{b}/{prefix}/{sub}/{n} {os.path.getsize(p):,} B', file=sys.stderr)
PY
echo "PATH-STORE-PHASE0-DONE -> $OUT_URI"
