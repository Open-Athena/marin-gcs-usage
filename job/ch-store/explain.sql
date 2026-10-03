-- Does a filter's candidate read use the `by_name` projection, and the as-of predicate prune partitions?
EXPLAIN indexes = 1 SELECT path, size FROM nodes
WHERE name IN (SELECT l FROM names WHERE l LIKE '%safetensors%') AND vf <= toDateTime('2026-09-04 00:00:00', 'UTC') AND vt > toDateTime('2026-09-04 00:00:00', 'UTC');
