-- Experiment B: the latest scan (2026-10-01, the bench truth's generation) as
-- `dt_cloud.bench.ch`'s tables, from `dt-cloud bench-ch-export`'s parquet in
-- /data/in/ch/.
CREATE TABLE IF NOT EXISTS names (nid UInt32, l String, INDEX tl l TYPE text(tokenizer = ngrams(3)))
ENGINE = MergeTree ORDER BY l;

CREATE TABLE IF NOT EXISTS nodes (pre UInt32, post UInt32, depth UInt8, nid UInt32, b Int64, o Int64, path String CODEC(ZSTD(3)))
ENGINE = MergeTree ORDER BY pre;

CREATE TABLE IF NOT EXISTS nodes_by_name (nid UInt32, pre UInt32, post UInt32, depth UInt8, b Int64, o Int64, path String CODEC(ZSTD(3)))
ENGINE = MergeTree ORDER BY (nid, pre);

INSERT INTO names SELECT nid, l FROM file('ch/names.parquet') SETTINGS max_insert_threads = 8;
INSERT INTO nodes SELECT pre, post, depth, nid, b, o, path FROM file('ch/nodes-*.parquet') SETTINGS max_insert_threads = 8;
INSERT INTO nodes_by_name SELECT nid, pre, post, depth, b, o, path FROM file('ch/nodes-*.parquet') SETTINGS max_insert_threads = 8;
OPTIMIZE TABLE names FINAL;
