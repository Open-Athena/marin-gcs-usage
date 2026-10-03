-- The store on disk: per table and partition (nodes: 210601 = the open versions), projections included.
SELECT table, partition, sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS disk, sum(bytes_on_disk) AS bytes, count() AS parts
FROM system.parts WHERE active AND database = currentDatabase() GROUP BY table, partition ORDER BY table, partition;
SELECT table, name, formatReadableSize(sum(bytes_on_disk)) AS disk FROM system.projection_parts WHERE active AND database = currentDatabase() GROUP BY table, name;
SELECT at, sign, count() AS n FROM changes WHERE at >= '2026-09-29' GROUP BY at, sign ORDER BY at, sign;
