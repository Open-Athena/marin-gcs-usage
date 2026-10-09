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
