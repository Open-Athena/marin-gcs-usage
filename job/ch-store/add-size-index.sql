-- A store created before `size_mm` (schema.NODES): add and build it (a mutation writing the index files only).
ALTER TABLE nodes ADD INDEX IF NOT EXISTS size_mm size TYPE minmax GRANULARITY 1;
ALTER TABLE nodes MATERIALIZE INDEX size_mm SETTINGS mutations_sync = 2;
