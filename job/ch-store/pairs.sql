-- How far an ingest's pairing has come: key ranges finished, their rows and seconds.
SELECT count() AS done, sum(read_rows) AS rows, round(avg(query_duration_ms) / 1000) AS avg_s, max(event_time) AS last
FROM system.query_log WHERE type = 'QueryFinish' AND query LIKE concat('INSERT INTO ingest_fan_', {tag:String}, '%') AND event_time > now() - INTERVAL 3 HOUR;
