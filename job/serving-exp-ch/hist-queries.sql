-- Experiment B: history queries over the SCD-2 tables. Scans are indices
-- (0 = 2026-09-27 … 6 = 2026-10-03); a row holds at s iff vf <= s < vt.
-- Run cold (page cache + ClickHouse caches dropped) and warm; each statement
-- prints its time (`clickhouse-client --time`). Outputs are capped (LIMIT) so
-- the timing is the engine's.
SET max_threads = 8;

-- q1 as-of: the store root's buckets at 2026-09-28 (s = 1)
SELECT path, b, o FROM dir_hist WHERE depth = 1 AND vf <= 1 AND vt > 1 ORDER BY b DESC FORMAT Null;

-- q2 as-of: a bucket's children at 2026-09-28 (dirs, plus the objects directly in it)
SELECT path, b, o FROM (
    SELECT path, b, o FROM dir_hist WHERE depth = 2 AND startsWith(path, 'marin-us-central2/') AND vf <= 1 AND vt > 1
    UNION ALL
    SELECT path, size AS b, 1 AS o FROM obj_hist WHERE depth = 2 AND startsWith(path, 'marin-us-central2/') AND vf <= 1 AND vt > 1
) ORDER BY b DESC FORMAT Null;

-- q3 as-of: a deep dir's children at 2026-09-28 (the bucket's largest dir)
SELECT path, b, o FROM (
    SELECT path, b, o FROM dir_hist WHERE depth = 3 AND startsWith(path, {deep:String} || '/') AND vf <= 1 AND vt > 1
    UNION ALL
    SELECT path, size AS b, 1 AS o FROM obj_hist WHERE depth = 3 AND startsWith(path, {deep:String} || '/') AND vf <= 1 AND vt > 1
) ORDER BY b DESC FORMAT Null;

-- q4 diff at the root, adjacent scans (10-02 → 10-03: s 5 → 6), per bucket
SELECT path, sumIf(b, vf <= 5 AND vt > 5) AS b0, sumIf(b, vf <= 6 AND vt > 6) AS b1 FROM dir_hist
WHERE depth = 1 AND vf <= 6 AND vt > 5 GROUP BY path HAVING b0 != b1 FORMAT Null;

-- q5 diff at the root, 6 days apart (09-27 → 10-03: s 0 → 6)
SELECT path, sumIf(b, vf <= 0 AND vt > 0) AS b0, sumIf(b, vf <= 6 AND vt > 6) AS b1 FROM dir_hist
WHERE depth = 1 GROUP BY path HAVING b0 != b1 FORMAT Null;

-- q6 diff at a bucket, adjacent scans: its children whose rollup changed (dirs and direct objects)
SELECT path, sumIf(b, vf <= 5 AND vt > 5) AS b0, sumIf(b, vf <= 6 AND vt > 6) AS b1 FROM (
    SELECT path, vf, vt, b FROM dir_hist WHERE depth = 2 AND startsWith(path, 'marin-us-central2/') AND vt > 5 AND vf <= 6
    UNION ALL
    SELECT path, vf, vt, size AS b FROM obj_hist WHERE depth = 2 AND startsWith(path, 'marin-us-central2/') AND vt > 5 AND vf <= 6
) GROUP BY path HAVING b0 != b1 ORDER BY abs(b1 - b0) DESC FORMAT Null;

-- q7 diff at a bucket, 6 days apart
SELECT path, sumIf(b, vf <= 0 AND vt > 0) AS b0, sumIf(b, vf <= 6 AND vt > 6) AS b1 FROM (
    SELECT path, vf, vt, b FROM dir_hist WHERE depth = 2 AND startsWith(path, 'marin-us-central2/')
    UNION ALL
    SELECT path, vf, vt, size AS b FROM obj_hist WHERE depth = 2 AND startsWith(path, 'marin-us-central2/')
) GROUP BY path HAVING b0 != b1 ORDER BY abs(b1 - b0) DESC FORMAT Null;

-- q8 delta-only: every object version opened or closed in (0, 6] under a bucket, rolled to its child of the bucket
SELECT arrayStringConcat(arraySlice(splitByChar('/', path), 1, 2), '/') AS child,
       sumIf(size, vf > 0 AND vf <= 6) - sumIf(size, vt > 0 AND vt <= 6) AS db, countIf(vf > 0 AND vf <= 6) - countIf(vt > 0 AND vt <= 6) AS dobj
FROM obj_hist WHERE depth >= 2 AND startsWith(path, 'marin-us-central2/') AND ((vf > 0 AND vf <= 6) OR (vt > 0 AND vt <= 6 AND vt != 255))
GROUP BY child ORDER BY abs(db) DESC FORMAT Null;

-- q9 filtered diff at the root: objects matching `checkpoints` (full path), net change per bucket, 0 → 6
SELECT splitByChar('/', path)[1] AS bucket,
       sumIf(size, vf > 0 AND vf <= 6) - sumIf(size, vt > 0 AND vt <= 6) AS db
FROM obj_hist WHERE ((vf > 0 AND vf <= 6) OR (vt > 0 AND vt <= 6 AND vt != 255)) AND position(lowerUTF8(path), 'checkpoints') > 0
GROUP BY bucket FORMAT Null;

-- q10 filtered diff at a bucket: objects matching `step-` under it, net change per child, 5 → 6
SELECT arrayStringConcat(arraySlice(splitByChar('/', path), 1, 2), '/') AS child,
       sumIf(size, vf = 6) - sumIf(size, vt = 6) AS db
FROM obj_hist WHERE startsWith(path, 'marin-us-central2/') AND (vf = 6 OR vt = 6) AND position(lowerUTF8(path), 'step-') > 0
GROUP BY child FORMAT Null;

-- q11 series: bytes over time for one path (the bucket's largest dir), every scan
SELECT s, b FROM dir_hist ARRAY JOIN range(vf, least(vt, 7)) AS s WHERE depth = 2 AND path = {deep:String} ORDER BY s FORMAT Null;

-- q12 series: bytes over time for every bucket
SELECT path, s, b FROM dir_hist ARRAY JOIN range(vf, least(vt, 7)) AS s WHERE depth = 1 ORDER BY path, s FORMAT Null;
