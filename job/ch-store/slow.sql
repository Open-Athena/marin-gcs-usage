-- The slowest recent queries: seconds, rows / bytes read, memory, the query's head.
SELECT round(query_duration_ms / 1000, 2) AS s, read_rows, formatReadableSize(read_bytes) AS read, formatReadableSize(memory_usage) AS mem,
       substring(replaceRegexpAll(query, '\\s+', ' '), 1, 300) AS q
FROM system.query_log WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL {mins:UInt32} MINUTE AND query NOT LIKE 'INSERT%'
ORDER BY query_duration_ms DESC LIMIT 12;
