-- A store created before `parent` / `by_parent` (schema.NODES): add and build them (mutations).
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS parent String DEFAULT if(position(path, '/') = 0, '', substring(path, 1, length(path) - position(reverse(path), '/'))) CODEC(ZSTD(3)) AFTER name;
ALTER TABLE nodes MATERIALIZE COLUMN parent SETTINGS mutations_sync = 2;
ALTER TABLE nodes ADD PROJECTION IF NOT EXISTS by_parent (SELECT * ORDER BY depth, parent, size);
ALTER TABLE nodes MATERIALIZE PROJECTION by_parent SETTINGS mutations_sync = 2;
