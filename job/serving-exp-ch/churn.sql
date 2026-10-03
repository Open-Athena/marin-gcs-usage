-- Experiment B: per-scan churn of the SCD-2 tables. For each scan s: rows
-- present, versions opened at s (new or changed paths) and closed at s
-- (deleted or changed), as fractions of the rows present at s.
SET max_threads = 8;
SELECT 'obj' AS t, p.s AS s, present, opened, closed, round(opened / present, 5) AS c_open, round(closed / present, 5) AS c_close
FROM (SELECT s, count() AS present FROM obj_hist ARRAY JOIN range(vf, least(vt, (SELECT max(s) + 1 FROM scans))) AS s GROUP BY s) AS p
LEFT JOIN (SELECT vf AS s, count() AS opened FROM obj_hist GROUP BY s) AS a ON a.s = p.s
LEFT JOIN (SELECT vt AS s, count() AS closed FROM obj_hist WHERE vt != 255 GROUP BY s) AS z ON z.s = p.s
ORDER BY s;

SELECT 'dir' AS t, p.s AS s, present, opened, closed, round(opened / present, 5) AS c_open, round(closed / present, 5) AS c_close
FROM (SELECT s, count() AS present FROM dir_hist ARRAY JOIN range(vf, least(vt, (SELECT max(s) + 1 FROM scans))) AS s GROUP BY s) AS p
LEFT JOIN (SELECT vf AS s, count() AS opened FROM dir_hist GROUP BY s) AS a ON a.s = p.s
LEFT JOIN (SELECT vt AS s, count() AS closed FROM dir_hist WHERE vt != 255 GROUP BY s) AS z ON z.s = p.s
ORDER BY s;

SELECT 'obj' AS t, count() AS versions, round(count() / (SELECT count() FROM obj_raw WHERE s = (SELECT max(s) FROM scans)), 4) AS scan_equiv FROM obj_hist;
SELECT 'dir' AS t, count() AS versions, round(count() / (SELECT count() FROM dirr_raw WHERE s = (SELECT max(s) FROM scans)), 4) AS scan_equiv FROM dir_hist;
