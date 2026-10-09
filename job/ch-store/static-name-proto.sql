-- Prototype suffix-ordered postings for every suffix starting with one of a few
-- 3-character prefixes (complete for any query starting with one of them).
-- One row per (suffix position >= 3 chars long, postings version), the version
-- carrying its closure time `vt` (2106 while open).
DROP TABLE IF EXISTS default.sx_names;
DROP TABLE IF EXISTS default.sx_rows;
DROP TABLE IF EXISTS default.sx_proto;

CREATE TABLE default.sx_names ENGINE = MergeTree ORDER BY l AS
SELECT DISTINCT l FROM default.name_spans WHERE multiSearchAny(l, ['541', 'nk0', '48.', 'gof']);

CREATE TABLE default.sx_rows ENGINE = MergeTree ORDER BY (name, depth, path, usr, vf) AS
SELECT n.name AS name, n.depth AS depth, n.path AS path, n.usr AS usr, n.vf AS vf,
       if(c.vt = toDateTime(0, 'UTC'), toDateTime('2106-01-01 00:00:00', 'UTC'), c.vt) AS vt, n.size AS size, n.n_files AS n_files
FROM (SELECT name, depth, path, usr, vf, size, n_files FROM default.m_nodes WHERE name IN (SELECT l FROM default.sx_names)) AS n
LEFT JOIN (SELECT name, depth, path, usr, vf, vt FROM default.m_closures WHERE name IN (SELECT l FROM default.sx_names)) AS c
  USING (name, depth, path, usr, vf)
SETTINGS join_use_nulls = 0, join_algorithm = 'grace_hash', grace_hash_join_initial_buckets = 16;

CREATE TABLE default.sx_proto (s String, depth UInt8, path String, usr LowCardinality(String), vf DateTime('UTC'), vt DateTime('UTC'), size Int64, n_files Int64)
ENGINE = MergeTree ORDER BY (s, path, usr, vf) SETTINGS index_granularity = 1024;

INSERT INTO default.sx_proto
SELECT substringUTF8(r.name, p, 24) AS s, depth, path, usr, vf, vt, size, n_files
FROM default.sx_rows AS r
ARRAY JOIN range(1, toUInt64(greatest(lengthUTF8(r.name) - 1, 1))) AS p
WHERE substringUTF8(r.name, p, 3) IN ('541', 'nk0', '48.', 'gof');

SELECT 'sx_rows', count() FROM default.sx_rows;
SELECT table, sum(rows), sum(data_compressed_bytes), sum(data_uncompressed_bytes) FROM system.parts
WHERE active AND database = 'default' AND table IN ('sx_proto', 'sx_rows') GROUP BY table;
