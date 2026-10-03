#!/bin/bash
# On the ClickHouse VM: load one fetched date (/data/in/<date>/) as scan S into
# obj_raw / dir_raw, timing each, then drop its files.
#   load-scan.sh S DATE
set -euo pipefail
s=$1; d=$2
q() { clickhouse-client --time --progress=off -q "$1"; }
q "INSERT INTO scans VALUES ($s, '$d')"
echo "obj $d: $(q "INSERT INTO obj_raw SELECT concat(bucket, '/', name), $s, size_bytes, toUnixTimestamp64Milli(created), storage_class_id FROM file('$d/obj/*/*.parquet') SETTINGS max_insert_threads = 8" 2>&1)s"
echo "dir $d: $(q "INSERT INTO dir_raw SELECT fp, $s, b, o, coalesce(c2, 0), coalesce(c3, 0), coalesce(c4, 0) FROM file('$d/dir-stats.parquet') SETTINGS max_insert_threads = 8" 2>&1)s"
rm -rf /data/in/$d
