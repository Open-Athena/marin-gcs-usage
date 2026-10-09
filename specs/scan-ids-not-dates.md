# Scans are not dates: sub-daily scan ids everywhere, a short `?d=` on every page

From the gcs session (2026-10-09), on Ryan's direction: "we should not be treating dates as PKs! we should have option to trigger (or switch cron to) multiple scans per day. cw already does this so if you added code … to bake in 'date == scan', that's wrong and should be removed. I'm fine to leave the params BC (e.g. date maps to latest scan for that date) and we can generalize that to where we always pick that latest scan matching a provided strptime-ish slug. param `date` should be shortened (have use-prms canonicalize it to a new shorter key+val str), and canonical repr should drop leading '20' from years."

He also said "some of the above should go US too": this spec covers the `cloud` (upstream) part. gcs's own job (`job/run.sh` keyed by `DATE`, `listing/$DATE/`) is the gcs session's, done there in parallel (§4).

## 1. A scan's identity is its scan id, never its date

A scan id is `YYYY-MM-DD` or `YYYY-MM-DDTHHMM` (cw's `SNAP_ID`, `job/cw-run.sh`: `$(date -u +%Y-%m-%dT%H%M)`). The path store already keys on it (`index_schema.date` is "snapshot date / scan id"; `api/diff.ts` `SCAN_RE`; `og/data.ts` `scanOfSel`). Audit `cloud` for code that assumes one scan per date, and fix it:

- **Static name search** (yours): `job/static-daily.sh DATE`, `deltas/<D>/`, `manifests/<D>.json`, `state/<D>/`, the "next scan" check (exit 3), `dt-cloud static-names daily …`, R2 keys, the catalog's per-date rows, `api/name-summary*` (`date`, `from`), `src/nameModel.ts` (`date` keys, `DAILY_SOURCES`, registry rows `{date, …}`), `_lib/nameSummaryStatic.ts`, `_lib/staticFilter.ts` ("every scan of its generation"). Each should take a scan id; two scans on one day are two deltas/manifests.
- Anything else that parses a scan with a date-only regex, or derives "the date's scan" by string equality, should use one shared resolver (§2).

## 2. A selection resolves to the latest scan matching its slug

One resolver, shared by Functions and the client: given a slug and the indexed scan ids, return the **latest scan whose id starts with the slug's expansion**:

| slug | matches |
|---|---|
| `261009` / `2026-10-09` | the latest `2026-10-09…` scan (the bare date if that's all there is) |
| `261009-12` / `2026-10-09T12` | the latest `2026-10-09T12…` scan |
| `261009-1200` / `2026-10-09T1200` | exactly that scan |

BC: existing links (`?d=2026-10-09`, `?date=2026-10-09`) keep working and mean "the latest scan of that day".

## 3. One short param, canonicalized by use-prms

The map's `?d=` already canonicalizes to the compact form (`261002`, `261002-1200`; `src/scan.ts` `encodeSel`/`decodeSel`). Every page that selects a scan uses the same key and codec:

- `/names?date=2026-10-09&name=gof` → `/names?d=261009&name=gof` (`from` → the same compact form, or the map's `d` look-back syntax if that fits). Decode accepts the old `date`/ISO forms and rewrites the URL to the canonical one.
- The canonical form drops the leading `20` of the year, everywhere a scan appears in a URL.
- The APIs can keep accepting ISO ids; the client sends whatever the resolver resolved.

## 4. gcs's side (FYI, done in the gcs session)

`job/run.sh` adopts cw's pattern: `SNAP_ID=${SNAP_ID:-$(date -u +%Y-%m-%dT%H%M)}`, keyed through `listing/$SNAP_ID/`, `snapshots/$SNAP_ID/`, `index-sync … $SNAP_ID`, healthcheck / warm-cache / ch-ingest / index-gc. `static-daily.sh` is then called with the scan id. If any `dt-cloud` command it calls (`job submit-listing -d`, `path-index -d`, `healthcheck -d`, `ch-ingest -d`, `index-dir`) refuses a `T`-suffixed id, gcs will fix it as a `[cloud]` commit for you to pick, or tell you.

## Done when

- Two gcs scans on one day are both indexed, browsable on the map, and searchable on `/names`, each by its own id. The static daily append builds a delta per scan.
- `?d=261009` resolves to the day's latest scan on every page; old `date=` links redirect to `d=`.
- Tests cover the resolver table (§2) and the codec round-trip (§3).

## Remaining (handoff, 2026-10-09)

### Done
- **§2–3 (merged to `cloud`):** `site/src/scanSlug.ts` is the one resolver:
  - `resolveScan`, `latestScan`, `scanMatches`, `scanNeighbors` and `isScanId`;
  - the `?d=` codec: `encodeSel`/`decodeSel`, `selOf`, `canonicalSel`, `useScanSel`.
  - Slugs are dashless `YYMMDD[HH[MM]]`. Legacy `-HH`/`-HHMM`, `T` and ISO spellings still decode; the grammar is in `scanSlug.ts`.
  - A slug that matches nothing is a miss: the pages show "No scan matches X" with links to the nearest scans, and the APIs return 404 `{error}` (`functions/_lib/scanArg.ts`).
  - Python: `dt_cloud/scan_id.py` (`SCAN_ID`, `slug_prefix`, `resolve_slug`, `scan_slug`, `latest_scan`). `healthcheck -d` fails when its slug matches nothing.
- **Branch `scan-slug-3` (local, not merged):**
  - `85883570` / `fdfe3df2`: a date-only scan's exact slug is its **midnight**, `YYMMDD0000`.
    - Matching reads `YYYY-MM-DD` as `T0000` (`scanKey` / `scanUnder`; Python `scan_key`).
    - Every picker writes `exactPrefix(id)`, and `scanArg`'s D1 query matches the same way.
    - `check_order` refuses a date together with its `T0000`, so the key can't collide with a real scan.
    - This already fixes the `?f=tomat&d=26100900` repro: `26100900` matches the date-only scan, and the "← 10/9" link writes `2610090000`.
    - The gcs digest links to a date-only scan as `…0000`; the goldens differ only by that.
  - `fa977ed2`: a new refusal code, `term-too-common`. A heavy literal without `FILTER_STATIC_HEAVY` is `term-too-common`, not `scan-not-indexed` (`declined()` in `staticFilter.ts`).
  - `c1a59162`: filter refusals render inline, with no "view failed", no retry and no status, on the map, the Diff and the size chart (`apiError` / `refusalOf` in `filterCaps.ts`).
  - `be53b663`: a filtered view with no `d` on an indexed-only deployment opens on the newest scan the static index covers.
    - The covered list comes from `/api/filter-scans`; the pieces are `floatingScan`, `selectScan` and `pendingNote`.
    - Checked on dev only with every scan indexed: T1236 was already appended, so no note showed. The "newest scan unindexed" case is covered by tests only.

### Left
1. **Real start times for date-only scans (coordinator's preferred design over midnight).**
   - The design:
     - A date-only scan's effective time is its real start, about 04:30Z for 2026-10-09.
     - Its canonical slug is that minute (`2610090430`), and the hour/minute slugs match it.
     - `261009` stays the day's latest scan by real time.
     - Pickers, nearest-scan links and labels use it, and the id stays `2026-10-09`.
     - When the time is unknown, fall back to the midnight form above.
   - Where the time is recorded: each bucket listing's `listing/<id>/<bucket>/_SUCCESS.json` carries `started` (`disk_tree/find/bulk*.py`); the scan's start is the earliest across its buckets.
     - `meta.json` has only `published` (the end of the job).
     - The `index_schema.gen` stamp is the time the job started, but a reprocess re-stamps it, so it isn't the scan's time.
   - Suggested plan:
     - `path-index` writes `meta.started` (the earliest listing `started`).
     - A backfill command stamps existing metas; the gcs session runs it, since it writes GCS.
     - The scan-list API exposes `started` for date-only scans on days with more than one scan. Only those need it; a single-scan day's `261009` is already unique.
     - `scanKey` takes that time where it's known, and so do the TS and Python resolvers and `scanArg`. `scanArg` needs it outside D1, from meta.
2. **Dropdown (`ScanCombobox`, `ScanPicker`, the `?d=` disambiguation strip):**
   - On a day with more than one scan, every row is a time. The date-only row shows its real start (2026-10-09 → local "12:30a"), or "(time unknown)" when there's none.
   - Order the rows by real time.
   - A single-scan day keeps "10/8".
   - Pinning the date-only row must pin exactly it. `fdfe3df2` makes `setDP` write `2610090000`; confirm on dev in Chrome, which hasn't been done yet.
3. **The `26100900` no-match links:** fixed under the midnight design (tests in `scanSlug.test.ts` and `noScanMatch.test.ts`). Re-key them to the real time with item 1.
4. **Timezone leak** ("no scan matches date=2026-10-09T0836"):
   - No client path builds an id from local time (`fmtScan` is display-only; `dateOfX` and the pickers pass UTC ids).
   - Not reproduced yet. Check on dev with the network panel open: the 8:36a entry, the diff pin, the dropdown and the brush.
5. **The disambiguation strip** ("?d=261009 matches 2 scans"): its buttons call `setDP(id)`, which is exact since `fdfe3df2`; verify on dev.

### Throwaway worktrees and branches to delete
`wt/scan-slug-gcs` (`scan-slug-gcs`), `wt/scan-slug-2-gcs` (`scan-slug-2-gcs`) and `wt/scan-slug-3-gcs` (`scan-slug-3-gcs`; the current dev deploy, `gcs-dev` → 906c1102c), plus the merged `wt/scan-slug` and `wt/scan-slug-2`.
