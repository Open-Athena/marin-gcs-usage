# Staging "as of" a scan

**Status:** implemented on the `as_of` branch (site + executor + migrations `cw/0014` / `gcs/0038`); the prod backfill below was applied 2026-10-09 (gcs prod D1, after `0038`/`0039`): all 6,632 open-plan items matched the marks rule (`2026-08-24` 4,567 · `08-25` 268 · `08-27` 31 · `08-28` 1,549 · `08-29` 4 · `08-31` 86 · `09-01` 127; every `listing/<as_of>/<bucket>/` checked present), none the `added_ts` rule.

## Rule

A staging is "as of" a scan (a scan date like `2026-10-07`), not a datetime. `plan_items.as_of` records it per item:

- `POST /api/plans/stage` takes optional `as_of` (must be a scan the site lists, i.e. `index_schema` `variant = 'path'`), default the latest scan. The UI sends the scan on screen (`?d=`). A re-staged prefix keeps its first `as_of` (`INSERT OR IGNORE`); an ancestor that absorbs staged descendants gets the new gesture's `as_of`.
- Dispatch snapshots `as_of` into `plan.json` (`{ "as_of": { "<prefix>": "<scan>" } }`, keyed like `sweep`; items with NULL `as_of` are absent and the dispatch scan stands in).
- gcs `dt-cloud sweep manifest` (`sweep_manifest.py`): an object under an item whose `as_of` ≠ the dispatch scan stays in the manifest only if that item's `as_of` listing (`listing/<as_of>/<bucket>/`) has it with the same generation (or, where either listing has no generation, `created` within 1 s). The rest are counted as `skipped_after_as_of` (per bucket, in `total`, and the held items under `as_of` in `plan-summary.json`). Items as of the dispatch scan read nothing extra. Nested items with different scans are a union.
- cw `dt-cloud plan-sweep manifest` (`sweep.py`): same rule against `cw-l2/<as_of>/<bucket>.parquet`, identity = path + mtime.
- `sweep execute-reviewed` refuses a DR whose `plan-summary.json` `as_of` differs from what the current plan holds back ("dry-run the plan again"), so a DR from before the backfill can't drive a real run of re-dated items.

## Backfill for the existing open plan (applied 2026-10-09)

Rules:

- batches whose note says "restored from the retired marks ledger" or "keep-last-checkpoint marks of <date>": `as_of` = the earliest scan on/after the first `YYYY-MM-DD` in the note (`of 2026-08-24..28` → the earliest scan ≥ `2026-08-24`);
- every other item: the scan of its `added_ts` day (UTC), else the latest scan before that day.

List the scan dates first (and check their listings still exist — the manifest reads `listing/<as_of>/<bucket>/` and fails the run if a shard set is missing):

```bash
CLOUDFLARE_ACCOUNT_ID=… npx wrangler d1 execute oa-gcs-usage-auth --remote \
  --command "SELECT DISTINCT date FROM index_schema WHERE variant = 'path' ORDER BY date"
gcloud storage ls gs://oa-gcs-usage-dvx/listing/
```

The shared CTE (`target`: one row per open-plan item still without `as_of`):

```sql
WITH RECURSIVE
  scans(date) AS (SELECT DISTINCT date FROM index_schema WHERE variant = 'path'),
  open_plan(id) AS (SELECT id FROM plans WHERE state = 'open' ORDER BY created_ts DESC LIMIT 1),
  marks(id, note) AS (
    SELECT id, note FROM stage_batches
    WHERE plan_id IN (SELECT id FROM open_plan)
      AND (note LIKE '%restored from the retired marks ledger%' OR note LIKE '%keep-last-checkpoint marks of %')
  ),
  -- the first `YYYY-MM-DD` in the note (`of 2026-08-24..28` → 2026-08-24)
  pos(id, note, i) AS (
    SELECT id, note, 1 FROM marks
    UNION ALL
    SELECT id, note, i + 1 FROM pos
    WHERE i < length(note) AND substr(note, i, 10) NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  mark_date(id, d) AS (
    SELECT id, substr(note, i, 10) FROM pos
    WHERE substr(note, i, 10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
  ),
  target(plan_id, prefix, batch_id, rule, as_of) AS (
    SELECT i.plan_id, i.prefix, i.batch_id,
      CASE WHEN md.d IS NOT NULL THEN 'mark ' || md.d
           WHEN m.id IS NOT NULL THEN 'mark (no date in note!)'
           ELSE 'added ' || date(i.added_ts, 'unixepoch') END,
      CASE WHEN md.d IS NOT NULL THEN (SELECT min(date) FROM scans WHERE date >= md.d)
           ELSE (SELECT max(date) FROM scans WHERE date <= date(i.added_ts, 'unixepoch')) END
    FROM plan_items i
    LEFT JOIN marks m ON m.id = i.batch_id
    LEFT JOIN mark_date md ON md.id = i.batch_id
    WHERE i.plan_id IN (SELECT id FROM open_plan) AND i.as_of IS NULL
  )
```

1. Preview (review every `mark (no date in note!)` row and any NULL `as_of` — no scan on/before the day — by hand):

   ```sql
   <CTE> SELECT rule, as_of, count(*) AS items FROM target GROUP BY rule, as_of ORDER BY as_of, rule;
   ```

2. Apply:

   ```sql
   <CTE> UPDATE plan_items
     SET as_of = (SELECT t.as_of FROM target t WHERE t.plan_id = plan_items.plan_id AND t.prefix = plan_items.prefix)
     WHERE (plan_id, prefix) IN (SELECT plan_id, prefix FROM target);
   ```

Verified on a seeded gcs-lineage SQLite (FKs ON): a `keep-last-checkpoint marks of 2026-08-24..28` batch with scans `08-23, 08-25, …` → `2026-08-25`; an item added 10-01 with scans `09-30, 10-02` → `2026-09-30`.

## Open questions

- "The scan of its `added_ts` day": an item staged on day D before that day's scan landed (~07:00 UTC + job time) actually saw scan D−1; taking D lets objects written between D−1's listing and the staging through. The stricter choice is the latest scan whose index was synced before `added_ts` (D1 doesn't record sync time; `index_schema.gen` might encode it).
- The `as_of` listings are held in memory per bucket (only rows under held items). An `as_of` on a very large prefix (tens of millions of objects) costs a few GB on the Batch VM.
- Held-back objects in a manifest dir still show up in `sweep execute` as drift (`drift_new_objects`) — the drift rule is untouched, so such a band stays staged after the real run (`unstageDeleted`).
- `deletion_runs` has no `skipped_after_as_of` column: the count lives in the run's `plan-summary.json` (and the job log), not in D1 / `/staged`.
