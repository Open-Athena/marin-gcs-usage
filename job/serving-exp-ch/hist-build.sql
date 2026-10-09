-- Experiment B: SCD-2 intervals from the raw scans (hist-ddl.sql; dir rollups from dir-rollup.sh). One pass
-- per table, aggregating in primary-key order (path, s), so memory stays
-- bounded: each path's scans → runs of identical, consecutive presence →
-- one [vf, vt) row per run. LAST is the newest loaded scan's index.
SET optimize_aggregation_in_order = 1, max_threads = 8;

INSERT INTO obj_hist
SELECT toUInt8(length(splitByChar('/', path))) AS depth, path,
       arr[st].1 AS vf, if(arr[en].1 = {last:UInt8}, 255, arr[en].1 + 1) AS vt,
       arr[st].2 AS size, arr[st].3 AS created, arr[st].4 AS sc
FROM (
    SELECT path, arraySort(x -> x.1, groupArray((s, size, created, sc))) AS arr,
           arrayFilter(i -> i = 1 OR arr[i].1 != arr[i - 1].1 + 1
                            OR (arr[i].2, arr[i].3, arr[i].4) != (arr[i - 1].2, arr[i - 1].3, arr[i - 1].4), arrayEnumerate(arr)) AS starts,
           arrayMap(k -> if(k < length(starts), starts[k + 1] - 1, length(arr)), arrayEnumerate(starts)) AS ends
    FROM obj_raw GROUP BY path
) ARRAY JOIN starts AS st, ends AS en;

INSERT INTO dir_hist
SELECT depth, path,
       arr[st].1 AS vf, if(arr[en].1 = {last:UInt8}, 255, arr[en].1 + 1) AS vt,
       arr[st].2 AS b, arr[st].3 AS o, arr[st].4 AS c2, arr[st].5 AS c3, arr[st].6 AS c4
FROM (
    SELECT depth, path, arraySort(x -> x.1, groupArray((s, b, o, c2, c3, c4))) AS arr,
           arrayFilter(i -> i = 1 OR arr[i].1 != arr[i - 1].1 + 1
                            OR (arr[i].2, arr[i].3, arr[i].4, arr[i].5, arr[i].6) != (arr[i - 1].2, arr[i - 1].3, arr[i - 1].4, arr[i - 1].5, arr[i - 1].6), arrayEnumerate(arr)) AS starts,
           arrayMap(k -> if(k < length(starts), starts[k + 1] - 1, length(arr)), arrayEnumerate(starts)) AS ends
    FROM dirr_raw GROUP BY depth, path
) ARRAY JOIN starts AS st, ends AS en;
