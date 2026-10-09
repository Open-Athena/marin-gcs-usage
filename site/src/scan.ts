import { useMemo } from 'react'
import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { useUrlAlias, useUrlState } from 'use-prms'
import { decodeSel, encodeScan, encodeSel, latestScan, legacyDateParam, legacyFromParam, mergeSel, scanMatches, scanNeighbors, scanParts, selSlug, type ScanSel } from './scanSlug'
import { storeUrl, type Store } from './stores'

// How often an unpinned tab re-checks for newly published scans.
export const SCANS_POLL_MS = 5 * 60_000


// Scan labels: drop the redundant year for the current one, so a list of
// same-year scans reads as `8/17` rather than `2026-08-17`. Scan ids are
// `YYYY-MM-DD`, or timed as `YYYY-MM-DDTHHMM`.
export function fmtScan(s: string, now = new Date()): string {
  const p = scanParts(s)
  if (!p) return s
  const { y, mo, d, hh, mm } = p
  if (!hh) {
    // Date-only ids are calendar dates, not instants —
    // rendering them through a timezone would shift some readers a day off.
    return Number(y) === now.getFullYear() ? `${Number(mo)}/${Number(d)}` : `${y}-${mo}-${d}`
  }
  // Timed ids are UTC instants; display in the viewer's local time,
  // 12-hour with a bare a/p ("8/19 6:08a"). The `?d=` token stays UTC (see
  // decodeScan) — display converts, the URL doesn't.
  const dt = new Date(Date.UTC(+y, +mo - 1, +d, +hh, +(mm ?? '0')))
  const h = dt.getHours()
  const time = `${h % 12 || 12}:${String(dt.getMinutes()).padStart(2, '0')}${h < 12 ? 'a' : 'p'}`
  const md = `${dt.getMonth() + 1}/${dt.getDate()}`
  return dt.getFullYear() === now.getFullYear()
    ? `${md} ${time}`
    : `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ${time}`
}

export { DAY, decodeScan, decodeSel, decodeSpan, encodeScan, encodeSel, encodeSpan, latestScan, nearestScan, resolveScan, scanMatches, scanNeighbors, scanTime, type ScanSel } from './scanSlug'

const selParam = { encode: encodeSel, decode: (e: string | undefined) => decodeSel(e) }

/** The page's scan selection (`?d=`), read through use-prms's alias hook so a
 * legacy `?date=<id>` / `?from=<id>` (or an ISO `?d=2026-10-06`) folds into
 * the one canonical `?d=` on mount (specs/scan-ids-not-dates.md §3). Writes go
 * through `useUrlState` so a scan pick is a history entry (back button). */
export function useScanSel(): [ScanSel | undefined, (v: ScanSel | undefined) => void] {
  const [sel] = useUrlAlias<ScanSel>({
    keys: ['d', 'date', 'from'],
    params: { d: selParam, date: legacyDateParam(), from: legacyFromParam() },
    merge: mergeSel,
  })
  const [, setSel] = useUrlState('d', selParam, true)
  return [sel, setSel]
}

/** A `?d=` slug that matches no scan: the slug as written (canonical when it
 * decoded), and the closest scans on either side to offer instead. */
export interface ScanMiss { slug: string; before: string | null; after: string | null }

/** The selection's miss, once the scan list has answered: an unparseable
 * value, or an end slug no scan matches. (A pinned start that misses is the
 * diff's own state — see `fromMiss`.) */
export function scanMiss(sel: ScanSel | undefined, scans: readonly string[], loaded: boolean): ScanMiss | null {
  if (!loaded || !sel) return null
  if (sel.invalid) return { slug: sel.invalid, before: null, after: null }
  if (!sel.d || latestScan(sel.d, scans)) return null
  return { slug: selSlug(sel), ...scanNeighbors(sel.d, scans) }
}

/** A pinned start (`from`) that matches no scan before `after`. */
export function fromMiss(from: string | undefined, after: string | null, scans: readonly string[]): ScanMiss | null {
  if (!from || !after) return null
  const earlier = scans.filter(s => s < after)
  return latestScan(from, earlier) ? null : { slug: encodeScan(from) ?? from, ...scanNeighbors(from, earlier) }
}

export interface Scan {
  asof: string | null
  /** `?d=` names no scan (then `asof` is null): what to say and offer. */
  miss: ScanMiss | null
  scans: string[]
  dMatches: string[]
  dP: string | undefined
  /** Pin the "after" scan; the latest scan (or undefined) clears the pin. */
  setDP: (v: string | undefined) => void
  /** Diff look-back in ms; undefined = the previous scan (or a pinned `from`). */
  span: number | undefined
  setSpan: (ms: number | undefined) => void
  /** "Before" pinned to a scan (decoded id); undefined = use `span`. Setting it
   *  clears `span` (the two "before" forms are mutually exclusive). */
  from: string | undefined
  setFrom: (v: string | undefined) => void
  /** Pin the "after" endpoint at the current scan, or release it to follow the
   *  latest scan. (Only meaningful while the page is on the latest scan; an
   *  older `asof` is already pinned.) */
  setEndPin: (pin: boolean) => void
  /** Pin + look-back in one URL write (a chart brush sets both). */
  setRange: (d: string | undefined, ms: number | undefined) => void
  scansQ: UseQueryResult<string[]>
}

// Shared scan resolution: `?d=YYMMDD` (a prefix of a scan id) pins a scan;
// absent means "latest" (a first-class state, so a parked tab follows new scans
// via the poll rather than freezing on the day it opened). Every scan-scoped
// page (home map, /users, /user/:id) uses this so a scan pin is one shareable,
// page-independent dimension. `scans` is newest-first, so the first prefix match
// is the newest. See specs/done/scan-param-all-pages.md.
/** The store's scan list (newest first) — the one definition every page
 * shares, so the poll and error handling are the same wherever it mounts.
 * The list polls so an unpinned tab discovers new scans on its own; the
 * per-scan payloads are immutable once published, so they never refetch.
 * Store-scoped key, so switching stores swaps the whole payload set. */
export function useScans(store: Store): UseQueryResult<string[]> {
  return useQuery<string[]>({
    queryKey: ['scans', store.key],
    // Throw on non-OK (e.g. a 401 from the data proxy with no session) so
    // react-query holds it as an error rather than handing the error body
    // downstream — `scans` then stays `[]` and the error surfaces as a sign-in
    // prompt rather than crashing `scans.map`.
    queryFn: async () => {
      const r = await fetch(storeUrl(`${store.base}/scans.json`, store))
      if (!r.ok) throw Object.assign(new Error(`scans: ${r.status}`), { status: r.status })
      return r.json()
    },
    refetchInterval: SCANS_POLL_MS,
  })
}

/** The scan list has answered and named nothing. The data Function lists a
 * snapshot only once its index rows are in D1 (`index_schema`), so an empty
 * list means no indexed snapshot exists for this store — the page has nothing
 * to fetch, and should say so rather than leave the map's skeleton up. A
 * pending or failed list is not "empty": the skeleton and the error strip
 * cover those. */
export const noScansYet = (q: { isSuccess: boolean; data?: string[] }): boolean =>
  q.isSuccess && q.data?.length === 0

export function useScan(store: Store): Scan {
  const [sel, setSel] = useScanSel()
  const scansQ = useScans(store)
  const scans = useMemo(() => scansQ.data ?? [], [scansQ.data])
  const dP = sel?.d
  const span = sel?.span
  const from = sel?.from
  const dMatches = useMemo(() => scanMatches(dP, scans), [dP, scans])
  // A `?d=` naming no scan is a miss, never the latest (or nearest) instead.
  const miss = useMemo(() => scanMiss(sel, scans, scansQ.isSuccess), [sel, scans, scansQ.isSuccess])
  const asof = miss ? null : sel?.invalid ? null : dP ? dMatches[0] ?? null : scans[0] ?? null
  // Write the {end, before} pair verbatim — `before` is a span OR a pinned
  // `from`, never both. Callers that pass a `d` equal to the latest scan mean
  // "float" and drop it; `setEndPin` is the one path that pins at latest.
  const write = (d: string | undefined, span0: number | undefined, from0: string | undefined) =>
    setSel(d || span0 || from0
      ? { ...(d ? { d } : {}), ...(span0 ? { span: span0 } : {}), ...(from0 ? { from: from0 } : {}) }
      : undefined)
  const setRange = (v: string | undefined, ms: number | undefined) =>
    write(v && v !== scans[0] ? v : undefined, ms, undefined)
  const setDP = (v: string | undefined) => write(v && v !== scans[0] ? v : undefined, span, from)
  const setSpan = (ms: number | undefined) => write(dP, ms, undefined)
  const setFrom = (v: string | undefined) => write(dP, undefined, v)
  const setEndPin = (pin: boolean) => write(pin ? asof ?? undefined : undefined, span, from)
  return { asof, miss, scans, dMatches, dP, setDP, span, setSpan, from, setFrom, setEndPin, setRange, scansQ }
}

/** `<optgroup>` rows for a scan picker: scans grouped by their displayed
 * day (`fmtScan`'s date part, viewer-local for timed ids), newest day
 * first, each option labelled by its time alone (`8:01a`) — a date-only scan
 * is its day's single, unlabelled-time entry. A list of sixty `9/16 8:01p`
 * rows read as noise; grouped, the day is said once. */
export function scanGroups(scans: string[], now = new Date()): { day: string; scans: { id: string; label: string }[] }[] {
  const out: { day: string; scans: { id: string; label: string }[] }[] = []
  for (const id of scans) {
    const f = fmtScan(id, now)
    const sp = f.indexOf(' ')
    const day = sp < 0 ? f : f.slice(0, sp)
    const label = sp < 0 ? f : f.slice(sp + 1)
    const last = out[out.length - 1]
    if (last && last.day === day) last.scans.push({ id, label })
    else out.push({ day, scans: [{ id, label }] })
  }
  return out
}
