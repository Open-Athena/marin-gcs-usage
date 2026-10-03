-- Experiment B: rows, on-disk and uncompressed bytes, parts per table; the text index's size.
SELECT table, sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS disk, formatReadableSize(sum(data_uncompressed_bytes)) AS raw, count() AS parts
FROM system.parts WHERE active AND database = currentDatabase() GROUP BY table ORDER BY table;
SELECT table, name, formatReadableSize(sum(data_compressed_bytes)) AS idx FROM system.data_skipping_indices WHERE database = currentDatabase() GROUP BY table, name;
SELECT formatReadableSize(value) AS rss FROM system.asynchronous_metrics WHERE metric = 'MemoryResident';
