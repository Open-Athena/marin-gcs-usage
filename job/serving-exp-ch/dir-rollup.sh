#!/bin/bash
# On the ClickHouse VM: recursive dir rollups per scan from `dir_raw`, which
# holds each dir's *direct* objects only (`dir-cache/dir-stats.parquet`; dirs
# with no direct objects are absent). Bottom-up, one depth at a time:
# rollup(x) = direct(x) + Σ rollup(children), so every row moves up one level
# per step instead of being copied to each ancestor (gcs's depth makes that
# ~10B ancestor rows). A work table `dw`, ordered by depth, holds each level's
# direct rows plus the rollups its children push up; a level whose keys
# wouldn't fit is grouped in Q passes split by a hash of the path (hashes only
# partition the work; nothing is keyed by them).
#   dir-rollup.sh [ROWS_PER_PASS]
set -euo pipefail
R=${1:-150000000}
q() { clickhouse-client --max_threads 8 --max_bytes_before_external_group_by 6000000000 -q "$1"; }
q "DROP TABLE IF EXISTS dirr_raw"
q "DROP TABLE IF EXISTS dw"
q "CREATE TABLE dirr_raw (depth UInt8, path String CODEC(ZSTD(3)), s UInt8, b Int64, o Int64, c2 Int64, c3 Int64, c4 Int64) ENGINE = MergeTree ORDER BY (depth, path, s)"
q "CREATE TABLE dw AS dirr_raw"
t0=$(date +%s)
q "INSERT INTO dw SELECT toUInt8(length(splitByChar('/', path))), path, s, b, o, c2, c3, c4 FROM dir_raw SETTINGS max_insert_threads = 8"
echo "work table at $(( $(date +%s) - t0 ))s"
dmax=$(q "SELECT max(depth) FROM dw")
for d in $(seq "$dmax" -1 1); do
  n=$(q "SELECT count() FROM dw WHERE depth = $d")
  Q=$(( n / R + 1 ))
  for k in $(seq 0 $((Q - 1))); do
    q "INSERT INTO dirr_raw SELECT $d, path, s, sum(b), sum(o), sum(c2), sum(c3), sum(c4) FROM dw
       WHERE depth = $d AND cityHash64(path) % $Q = $k GROUP BY path, s"
  done
  if [ "$d" -gt 1 ]; then
    q "INSERT INTO dw SELECT $d - 1, substring(path, 1, length(path) - position(reverse(path), '/')), s, b, o, c2, c3, c4
       FROM dirr_raw WHERE depth = $d"
  fi
  echo "depth $d: $n rows in $Q pass(es), at $(( $(date +%s) - t0 ))s"
done
q "DROP TABLE dw"
echo "rollups done in $(( $(date +%s) - t0 ))s"
