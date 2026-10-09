/** Scan ids, scan slugs and the `?d=` selection codec — the one definition the
 * client pages and the Functions share (DOM-free, React-free).
 *
 * A scan's identity is its **scan id**: date-only `YYYY-MM-DD`, or timed
 * `YYYY-MM-DDTHHMM` (UTC). A date is never a scan key — a
 * day may hold several scans (specs/scan-ids-not-dates.md).
 *
 * A **slug** is a user-facing spelling of a scan-id *prefix*; it resolves to
 * the **latest** indexed scan whose id starts with it (`resolveScan`):
 *
 *   261009        · 2026-10-09        → the latest `2026-10-09…` scan
 *   261009-12     · 2026-10-09T12     → the latest `2026-10-09T12…` scan
 *   261009-1200   · 2026-10-09T1200   → exactly that scan
 *
 * The canonical (URL) form is the compact one: the year's leading `20`
 * dropped, `-` before the time (`encodeScan`).
 */

const HOUR = 3600_000
export const DAY = 24 * HOUR

/** A scan id's shape (`isScanId` also checks the calendar). */
export const SCAN_ID_RE = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?$/

/** A real calendar date, and (if present) a real UTC time of day. */
function validParts(y: string, mo: string, d: string, hh?: string, mm?: string): boolean {
  const t = new Date(Date.UTC(+y, +mo - 1, +d))
  return t.getUTCFullYear() === +y && t.getUTCMonth() === +mo - 1 && t.getUTCDate() === +d
    && (hh === undefined || +hh <= 23) && (mm === undefined || +mm <= 59)
}

/** A full scan id: `YYYY-MM-DD` or `YYYY-MM-DDTHHMM`, calendar-valid. What the
 * APIs accept (they take exact ids; slugs resolve client-side). */
export function isScanId(v: unknown): v is string {
  if (typeof v !== 'string') return false
  const m = SCAN_ID_RE.exec(v)
  return !!m && m[1] !== '0000' && validParts(m[1], m[2], m[3], m[4], m[5])
}

/** A scan id's (or id-led string's) parts, leniently: `YYYY-MM-DD`, then an
 * optional `T`/space-separated `HH[:]MM` — the one parser the formatters and
 * `scanTime` share. Null when it doesn't lead with a date. */
export function scanParts(s: string): { y: string; mo: string; d: string; hh?: string; mm?: string } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):?(\d{2})?)?/.exec(s)
  return m ? { y: m[1], mo: m[2], d: m[3], ...(m[4] ? { hh: m[4] } : {}), ...(m[5] ? { mm: m[5] } : {}) } : null
}

/** A scan id's instant (UTC). Date-only ids read as midnight UTC. */
export const scanTime = (d: string): number => {
  const p = scanParts(d)
  return p ? Date.UTC(+p.y, +p.mo - 1, +p.d, +(p.hh ?? '0'), +(p.mm ?? '0')) : NaN
}

/** A scan-run id: `<scan id>-p<plan>/<YYYYMMDDTHHMMSSZ>` (a sweep run's dir). */
export const RUN_ID_RE = new RegExp(`^${SCAN_ID_RE.source.slice(1, -1)}-p\\d+\\/\\d{8}T\\d{6}Z$`)

/** Compact (canonical URL) form of a scan id or id prefix: `2026-10-09T1200`
 * → `261009-1200`, `2026-10-09` → `261009`. Anything else passes through. */
export const encodeScan = (v: string | undefined): string | undefined => {
  const m = v && /^\d{2}(\d{2})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(v)
  if (!m) return v || undefined
  const [, y, mo, d, hh, mm] = m
  return `${y}${mo}${d}` + (hh ? `-${hh}${mm ?? ''}` : '')
}

/** A slug → the scan-id prefix it names (ISO form), or undefined if it isn't
 * one. Spellings (all UTC):
 *   261009 · 261009-12 · 261009-1200 · 261009T12  (compact; canonical)
 *   2026-10-09 · 2026-10-09T12 · 2026-10-09T1200   (ISO; legacy links)
 *   10-9-12 · 10-9-12-00                           (M-D-H[-MM], current year)
 *   26-10-9-12 · 2026-10-9-12                      (year-first when the lead
 *                                                   component can't be a month) */
export const decodeScan = (e: string | undefined, now = new Date()): string | undefined => {
  if (!e) return undefined
  const pad = (n: string) => n.padStart(2, '0')
  const out = (y: string, mo: string, d: string, hh?: string, mm?: string) =>
    validParts(y, mo, d, hh, mm) ? `${y}-${mo}-${d}` + (hh ? `T${hh}${mm ?? ''}` : '') : undefined
  const compact = /^(\d{2})(\d{2})(\d{2})(?:[T-](\d{2})(\d{2})?)?$/.exec(e)
  if (compact) {
    const [, y, mo, d, hh, mm] = compact
    return out(`20${y}`, mo, d, hh, mm)
  }
  const iso = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(e)
  if (iso) {
    const [, y, mo, d, hh, mm] = iso
    return out(y, mo, d, hh, mm)
  }
  const parts = e.split(/[T-]/)
  if (parts.length < 2 || parts.length > 5 || parts.some(p => !/^(\d{1,2}|\d{4})$/.test(p))) return undefined
  // A lead component that can't be a month is a year (2- or 4-digit); 4-digit
  // components anywhere else are malformed.
  const yearFirst = parts[0].length === 4 || Number(parts[0]) > 12
  if (parts.slice(yearFirst ? 1 : 0).some(p => p.length > 2)) return undefined
  const y = yearFirst ? (parts[0].length === 4 ? parts[0] : `20${parts[0]}`) : String(now.getUTCFullYear())
  const [mo, d, hh, mm] = parts.slice(yearFirst ? 1 : 0)
  if (!mo || !d) return undefined
  return out(y, pad(mo), pad(d), hh && pad(hh), mm && pad(mm))
}

/** Every scan matching a decoded prefix, newest first (`scans` in any order). */
export function scanMatches(prefix: string | undefined, scans: readonly string[]): string[] {
  if (!prefix) return []
  return scans.filter(s => s.startsWith(prefix)).sort((a, b) => a < b ? 1 : a > b ? -1 : 0)
}

/** **The resolver**: the latest scan among `scans` (any order) whose id starts
 * with the decoded prefix — null when none does. Ids sort chronologically as
 * strings (a bare `YYYY-MM-DD` sorts before that day's `T` scans, i.e. reads
 * as its midnight). */
export function latestScan(prefix: string | undefined, scans: readonly string[]): string | null {
  if (!prefix) return null
  let best: string | null = null
  for (const s of scans) if (s.startsWith(prefix) && (best === null || s > best)) best = s
  return best
}

/** A slug (any spelling `decodeScan` reads) → the latest matching scan, or
 * null if the slug is malformed or matches nothing. */
export const resolveScan = (slug: string | undefined, scans: readonly string[], now = new Date()): string | null =>
  latestScan(decodeScan(slug, now), scans)

/** The scan nearest to `t` among `scans` (any order); null when empty. */
export const nearestScan = (scans: readonly string[], t: number): string | null => {
  let best: string | null = null
  for (const s of scans) if (!best || Math.abs(scanTime(s) - t) < Math.abs(scanTime(best) - t)) best = s
  return best
}

// ---- the `?d=` selection ----
//
// `?d=[end][-before]` — `end` is the "after" scan slug (absent = latest, a
// sticky state that follows new scans); `before` is the "before" endpoint,
// expressed *either* as a look-back span *or* as a second pinned scan:
//   ?d=-7d                     latest end, 7 days back (a floating window)
//   ?d=260904-0002             end pinned to the 9/4 00:02Z scan, default look-back
//   ?d=260904-0002-7d          end pinned, 7 days back
//   ?d=-260901-0002            latest end, START pinned to 9/1 (end floats, start fixed)
//   ?d=260904-0002-260901-0002 both endpoints pinned (a frozen window)
// Spans are `Nd`, `Nh`, or both (`6d12h`); a span resolves to the *nearest*
// scan. A pinned start (`from`) is a compact slug — `YYMMDD[-HHMM]`,
// distinguishable from a span (ends in d/h) and from the end scan's own
// `-HHMM` time (4 digits, never a 6-digit date). Span and `from` are mutually
// exclusive. The same key and codec serve every page that selects a scan
// (map, diff, /users, /names, OG cards).

export const encodeSpan = (ms: number): string => {
  const days = Math.floor(ms / DAY)
  const hours = Math.round((ms - days * DAY) / HOUR)
  return (days ? `${days}d` : '') + (hours ? `${hours}h` : '') || '0h'
}

export const decodeSpan = (s: string): number | undefined => {
  const m = /^(?:(\d+)d)?(?:(\d+)h)?$/.exec(s)
  if (!m || !s) return undefined
  const ms = (+(m[1] ?? 0)) * DAY + (+(m[2] ?? 0)) * HOUR
  return ms > 0 ? ms : undefined
}

export interface ScanSel {
  /** "After" scan-id prefix (decoded form, e.g. `2026-09-04T0002`); absent = latest. */
  d?: string
  /** "Before" as a look-back in ms; absent = the previous scan. Excludes `from`. */
  span?: number
  /** "Before" as a pinned scan-id prefix (decoded form). Excludes `span`. */
  from?: string
}

const SPAN_SUFFIX = /-(\d+d(?:\d+h)?|\d+h)$/
const FROM_SUFFIX = /-(\d{6}(?:-\d{4})?)$/

const sel = (d?: string, span?: number, from?: string): ScanSel | undefined =>
  d || span || from
    ? { ...(d ? { d } : {}), ...(span ? { span } : {}), ...(from && !span ? { from } : {}) }
    : undefined

export const encodeSel = (v: ScanSel | undefined): string | undefined => {
  if (!v) return undefined
  const d = encodeScan(v.d) ?? ''
  const before = v.from ? `-${encodeScan(v.from)}` : v.span ? `-${encodeSpan(v.span)}` : ''
  return d + before || undefined
}

export const decodeSel = (e: string | undefined, now = new Date()): ScanSel | undefined => {
  if (!e) return undefined
  // A whole-value ISO id (`2026-10-09`, `2026-10-09T1200`): the legacy
  // spelling, read before the suffix grammar (whose `-` splits would misparse it).
  if (/^\d{4}-/.test(e)) return sel(decodeScan(e, now))
  const sm = SPAN_SUFFIX.exec(e)
  const span = sm ? decodeSpan(sm[1]) : undefined
  let head = sm ? e.slice(0, sm.index) : e
  let from: string | undefined
  if (!span) {
    const fm = FROM_SUFFIX.exec(head)
    if (fm) { from = decodeScan(fm[1], now); head = head.slice(0, fm.index) }
  }
  const d = head ? decodeScan(head, now) : undefined
  return sel(d, span, from)
}

/** Fold `?d=` with the legacy keys it replaced — `?date=<id>` (the "after"
 * scan) and `?from=<id>` (a pinned start, /names) — into one selection; `d`'s
 * own parts win. The page then rewrites the URL to `?d=` alone. */
export function mergeSel(vals: { d?: ScanSel; date?: ScanSel; from?: ScanSel }): ScanSel | undefined {
  const { d, date, from } = vals
  return sel(d?.d ?? date?.d, d?.span, d?.from ?? (d?.span ? undefined : from?.from))
}

/** The legacy-key decoders `mergeSel` folds: each reads an ISO or compact id. */
export const legacyDateParam = (now = new Date()) => ({
  encode: (): undefined => undefined,
  decode: (v: string | undefined): ScanSel | undefined => sel(decodeScan(v, now)),
})
export const legacyFromParam = (now = new Date()) => ({
  encode: (): undefined => undefined,
  decode: (v: string | undefined): ScanSel | undefined => sel(undefined, undefined, decodeScan(v, now)),
})

/** A selection's "after" scan, resolved: the latest scan matching `d`, or the
 * latest scan when `d` is absent; null when `d` matches nothing (or `scans`
 * is empty). */
export function resolveAfter(s: ScanSel | undefined, scans: readonly string[]): string | null {
  if (s?.d) return latestScan(s.d, scans)
  let best: string | null = null
  for (const x of scans) if (best === null || x > best) best = x
  return best
}

/** A selection's "before" scan, given its resolved "after": a pinned `from`
 * resolves (latest match) among the scans before `after`; a span resolves to
 * the scan nearest `after − span`; neither = undefined (the caller's default,
 * usually the previous scan, or none). null = asked for but unresolvable. */
export function resolveBefore(s: ScanSel | undefined, after: string, scans: readonly string[]): string | null | undefined {
  const earlier = scans.filter(x => x < after)
  if (s?.from) return latestScan(s.from, earlier)
  if (s?.span) return nearestScan(earlier, scanTime(after) - s.span)
  return undefined
}

/** A URL's scan selection — `?d=` merged with the legacy `?date=` / `?from=`
 * (`mergeSel`). Pure: what `useScanSel` resolves, for code that reads the
 * search string itself (a router's params, an OG view). */
export function selOf(sp: URLSearchParams, now = new Date()): ScanSel | undefined {
  const get = (k: string) => sp.get(k) ?? undefined
  return mergeSel({
    d: decodeSel(get('d'), now),
    date: legacyDateParam(now).decode(get('date')),
    from: legacyFromParam(now).decode(get('from')),
  })
}

/** A URL's scan selection in canonical form (`selOf`, re-encoded compactly),
 * or undefined when it selects nothing (absent, or unparseable). What a
 * page's own URL canonicalizes to, so OG/share views key by the same string. */
export const canonicalSel = (sp: URLSearchParams, now = new Date()): string | undefined => encodeSel(selOf(sp, now))
