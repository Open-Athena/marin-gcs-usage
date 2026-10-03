# ClickHouse store: one interval table behind the Worker

**Status:** in progress (branch `ch-store`). Builds on [serving-options] §4.6 (SCD-2) and §6 (ClickHouse measured), and on [filter-query-service] §6.3 (`dt-cloud serve-query`), §7 (code layout) and §8 (serving tiers).

## 1. Goal

One ClickHouse database holds every scan of a deployment as **change records with intervals**: a row per version of a `(path, owner slice)`, valid from the scan that first saw it to the scan that first didn't. It subsumes the per-scan path store for serving: any scan's subtree, filter, diff and series answers come from it.

The Worker stays in front (auth, edge cache, fallback). It forwards the box-served routes to `dt-cloud serve-query -e ch`, which answers in the Worker's exact response shapes, and falls back to its own reads when the box errors, times out or declines.

One small VM ($200–300/month at most), suspendable. RAM page cache → the VM's persistent disk → GCS of record.

## 2. Schema

(to be filled as built)

## 3. Daily ingest

(to be filled as built)

## 4. Serving

(to be filled as built)

## 5. Worker hand-off

(to be filled as built)

## 6. Measured

(to be filled as measured)

[serving-options]: serving-options.md
[filter-query-service]: filter-query-service.md
