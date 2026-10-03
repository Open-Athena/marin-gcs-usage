-- Experiment B's history tables (specs/serving-options.md): every daily scan's
-- objects and dir rollups as raw rows, then SCD-2 intervals built from them.
-- Scan s is an index into `scans` (0 = the oldest loaded); an interval
-- [vf, vt) holds at scan s iff vf <= s < vt; vt = 255 is open (still present
-- at the newest scan).
CREATE TABLE IF NOT EXISTS scans (s UInt8, d Date) ENGINE = MergeTree ORDER BY s;

CREATE TABLE IF NOT EXISTS obj_raw (path String CODEC(ZSTD(3)), s UInt8, size Int64, created Int64, sc UInt8)
ENGINE = MergeTree ORDER BY (path, s);

CREATE TABLE IF NOT EXISTS dir_raw (path String CODEC(ZSTD(3)), s UInt8, b Int64, o Int64, c2 Int64, c3 Int64, c4 Int64)
ENGINE = MergeTree ORDER BY (path, s);

CREATE TABLE IF NOT EXISTS obj_hist (depth UInt8, path String CODEC(ZSTD(3)), vf UInt8, vt UInt8, size Int64, created Int64, sc UInt8)
ENGINE = MergeTree ORDER BY (depth, path, vf);

CREATE TABLE IF NOT EXISTS dir_hist (depth UInt8, path String CODEC(ZSTD(3)), vf UInt8, vt UInt8, b Int64, o Int64, c2 Int64, c3 Int64, c4 Int64)
ENGINE = MergeTree ORDER BY (depth, path, vf);
