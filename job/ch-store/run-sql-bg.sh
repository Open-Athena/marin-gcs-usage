#!/bin/bash
# On the ch-store VM: run /data/FILE.sql in the background → /data/FILE.log, ending with a "done" line.
f=${1:?FILE.sql}
nohup bash -c "clickhouse-client --time --multiquery --max_threads \$(nproc) < /data/$f > /data/${f%.sql}.log 2>&1; echo done >> /data/${f%.sql}.log" > /dev/null 2>&1 < /dev/null &
echo started
