-- What the server is doing: running queries, mutations, merges, and the store's tables.
SELECT round(elapsed) AS s, read_rows, written_rows, formatReadableSize(memory_usage) AS mem, substring(replaceRegexpAll(query, '\\s+', ' '), 1, 90) AS q
FROM system.processes WHERE query NOT LIKE '%system.processes%' ORDER BY elapsed DESC;
SELECT table, mutation_id, command, parts_to_do, is_done FROM system.mutations WHERE NOT is_done;
SELECT table, round(elapsed) AS s, round(progress, 2) AS p, num_parts, formatReadableSize(total_size_bytes_compressed) AS size FROM system.merges;
SELECT table, count() AS parts, sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS disk FROM system.parts WHERE active AND database = currentDatabase() GROUP BY table ORDER BY table;
