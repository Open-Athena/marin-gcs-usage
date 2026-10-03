-- Drop the store (a rebuild from the scans of record).
DROP TABLE IF EXISTS nodes SYNC;
DROP TABLE IF EXISTS changes SYNC;
DROP TABLE IF EXISTS names SYNC;
DROP TABLE IF EXISTS scans SYNC;
SELECT name FROM system.tables WHERE database = currentDatabase() AND name LIKE 'ingest%';
