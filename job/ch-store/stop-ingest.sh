#!/bin/bash
# On the ch-store VM (as root): stop an ingest loop, its container and its queries (a re-run recovers).
pkill -f ingest-days.sh
docker ps -q --filter ancestor="$(cat /data/image)" | xargs -r docker kill > /dev/null
sleep 2
clickhouse-client -q "KILL QUERY WHERE query LIKE '%ingest%' AND query NOT LIKE '%KILL QUERY%' SYNC" > /dev/null
echo stopped
