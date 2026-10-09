-- Experiment B: ClickHouse's memory: server RSS now, and the bench queries' peak per-query memory (last 3 hours).
SELECT formatReadableSize(value) AS rss FROM system.asynchronous_metrics WHERE metric = 'MemoryResident';
SELECT formatReadableSize(max(memory_usage)) AS peak_query, formatReadableSize(quantile(0.9)(memory_usage)) AS p90_query, count() AS queries
FROM system.query_log WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 3 HOUR AND query ILIKE '%FROM nodes_by_name%';
SELECT formatReadableSize(max(memory_usage)) AS peak_any FROM system.query_log WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 3 HOUR AND query NOT ILIKE 'INSERT%' AND query NOT ILIKE 'OPTIMIZE%';
