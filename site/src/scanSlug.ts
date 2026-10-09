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
 *   261009       · 2026-10-09        → the latest `2026-10-09…` scan
 *   26100912     · 2026-10-09T12     → the latest `2026-10-09T12…` scan
 *   2610091200   · 2026-10-09T1200   → exactly that scan
 *
 * A slug matching no scan is a miss — never the nearest or the latest scan
 * instead: the APIs answer 404, the pages say so (`scanNeighbors` names the
 * closest scans on either side to offer).
 *
 * The canonical (URL) form is the dashless compact one, `YYMMDD[HH[MM]]`
 * (`encodeScan`), which leaves `-` to `?d=`'s separators alone.
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

/** Compact (canonical URL) form of a scan id or id prefix — `YYMMDD[HH[MM]]`,
 * dashless: `2026-10-09T1236` → `2610091236`, `2026-10-09T12` → `26100912`,
 * `2026-10-09` → `261009`. Anything else passes through. */
export const encodeScan = (v: string | undefined): string | undefined => {
  const m = v && /^\d{2}(\d{2})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(v)
  if (!m) return v || undefined
  const [, y, mo, d, hh, mm] = m
  return `${y}${mo}${d}${hh ?? ''}${mm ?? ''}`
}

/** A slug → the scan-id prefix it names (ISO form), or undefined if it isn't
 * one. Spellings (all UTC):
 *   261009 · 26100912 · 2610091200                 (YYMMDD[HH[MM]]; canonical)
 *   261009-12 · 261009-1200 · 261009T12            (compact with a separator; legacy)
 *   2026-10-09 · 2026-10-09T12 · 2026-10-09T1200   (ISO; legacy)
 *   10-9-12 · 10-9-12-00                           (M-D-H[-MM], current year)
 *   26-10-9-12 · 2026-10-9-12                      (year-first when the lead
 *                                                   component can't be a month) */
export const decodeScan = (e: string | undefined, now = new Date()): string | undefined => {
  if (!e) return undefined
  const pad = (n: string) => n.padStart(2, '0')
  const out = (y: string, mo: string, d: string, hh?: string, mm?: string) =>
    validParts(y, mo, d, hh, mm) ? `${y}-${mo}-${d}` + (hh ? `T${hh}${mm ?? ''}` : '') : undefined
  // 6, 8 or 10 digits (an 8-digit run is YYMMDDHH, never YYYYMMDD), or the
  // legacy `-`/`T` before the time.
  const compact = /^(\d{2})(\d{2})(\d{2})(?:[T-]?(\d{2})(\d{2})?)?$/.exec(e)
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

/** A missed prefix's closest scans: the latest scan before everything it
 * names, and the earliest after — the links a "no scan matches" state offers.
 * (Ids sort chronologically as strings; `prefix + '~'` sorts after every id
 * the prefix names.) */
export function scanNeighbors(prefix: string, scans: readonly string[]): { before: string | null; after: string | null } {
  let before: string | null = null, after: string | null = null
  const end = `${prefix}~`
  for (const s of scans) {
    if (s < prefix && (before === null || s > before)) before = s
    if (s > end && (after === null || s < after)) after = s
  }
  return { before, after }
}

/** The scan nearest to `t` among `scans` (any order); null when empty. For a
 * look-back *span* (a duration, not a slug) and explicit UI picks (a chart
 * brush) only — a slug never snaps to its nearest scan. */
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
//   ?d=-7d                       latest end, 7 days back (a floating window)
//   ?d=2609040002                end pinned to the 9/4 00:02Z scan, default look-back
//   ?d=2609040002-7d             end pinned, 7 days back
//   ?d=-2609010002               latest end, START pinned to 9/1 00:02Z
//   ?d=2609040002-2609010002     both endpoints pinned (a frozen window)
//   ?d=26090412-260901           …each endpoint any slug: a day, an hour, a minute
// Canonical slugs are dashless (`encodeScan`), so `-` only ever separates the
// end, the start and the span. Spans are `Nd`, `Nh`, or both (`6d12h`); a
// span resolves to the *nearest* scan (a duration, not a slug). Span and
// `from` are mutually exclusive.
//
// Decoding (`decodeSel`) also reads every legacy spelling. In order:
//  1. A value leading with a 4-digit year (`2026-10-09`, `2026-10-09T1200`) is
//     one ISO slug, whole (ISO never carried a range).
//  2. A trailing `-<span>` (`-7d`, `-12h`, `-6d12h`) is the look-back.
//  3. The rest splits on `-` into tokens. A leading empty token (`-261001…`)
//     means the end floats. Each 6-, 8- or 10-digit token (or `YYMMDDT…`)
//     starts a slug; a 2- or 4-digit token right after a *6-digit* token is that
//     day's legacy `-HH` / `-HHMM` time (`261009-12` = 10/9's noon hour). No
//     canonical slug is 2 or 4 digits, so this never collides with a range:
//     `261009-12` is one slug (an hour), `261009-261001` two (a range),
//     `261009-1200-261001-0600` two legacy minutes.
//  4. One slug = the end (or, after a leading `-`, the start); two = end and
//     start. Anything else (`26100912-12`, three slugs, a range plus a span)
//     is not a selection — unless the whole head is a year-less spelling
//     `decodeScan` reads (`10-9`, `8-19-10`).
// A value that decodes to nothing is kept verbatim as `invalid`: it selects no
// scan (a miss, shown as such), never the latest.

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
  /** A `?d=` (or legacy key) value that names no selection, verbatim: it
   *  resolves to no scan, and re-encodes as itself. Excludes the rest. */
  invalid?: string
}

const SPAN_SUFFIX = /-(\d+d(?:\d+h)?|\d+h)$/
const SLUG_TOKEN = /^\d{6}(?:T\d{2}(?:\d{2})?)?$|^\d{8}$|^\d{10}$/

const sel = (d?: string, span?: number, from?: string): ScanSel | undefined =>
  d || span || from
    ? { ...(d ? { d } : {}), ...(span ? { span } : {}), ...(from && !span ? { from } : {}) }
    : undefined

export const encodeSel = (v: ScanSel | undefined): string | undefined => {
  if (!v) return undefined
  if (v.invalid) return v.invalid
  const d = encodeScan(v.d) ?? ''
  const before = v.from ? `-${encodeScan(v.from)}` : v.span ? `-${encodeSpan(v.span)}` : ''
  return d + before || undefined
}

/** `?d=`'s slugs, split per the grammar above (step 3–4): `[end, start]`
 * (either may be undefined), or null when the tokens don't form one. */
function selSlugs(head: string): [string | undefined, string | undefined] | null {
  const toks = head.split('-')
  const lead = toks[0] === ''
  const slugs: string[] = []
  for (const [i, t] of toks.entries()) {
    if (i === 0 && lead) continue
    if (SLUG_TOKEN.test(t)) slugs.push(t)
    else if (/^(?:\d{2}|\d{4})$/.test(t) && slugs.length && /^\d{6}$/.test(slugs[slugs.length - 1])) slugs[slugs.length - 1] += `-${t}`
    else return null
  }
  if (lead) return slugs.length === 1 ? [undefined, slugs[0]] : head === '' ? [undefined, undefined] : null
  return slugs.length === 1 ? [slugs[0], undefined] : slugs.length === 2 ? [slugs[0], slugs[1]] : null
}

export const decodeSel = (e: string | undefined, now = new Date()): ScanSel | undefined => {
  if (!e) return undefined
  const invalid: ScanSel = { invalid: e }
  if (/^\d{4}-/.test(e)) { const d = decodeScan(e, now); return d ? { d } : invalid }
  const sm = SPAN_SUFFIX.exec(e)
  const span = sm ? decodeSpan(sm[1]) : undefined
  const head = span ? e.slice(0, sm!.index) : e
  const slugs = selSlugs(head)
  if (!slugs) {
    // a year-less single slug (`10-9`, `8-19-10`), whole
    const d = decodeScan(head, now)
    return d ? sel(d, span) : invalid
  }
  const [end, start] = slugs
  if (start && span) return invalid
  const d = end ? decodeScan(end, now) : undefined, from = start ? decodeScan(start, now) : undefined
  if ((end && !d) || (start && !from)) return invalid
  return sel(d, span, from) ?? invalid
}

/** Fold `?d=` with the legacy keys it replaced — `?date=<id>` (the "after"
 * scan) and `?from=<id>` (a pinned start, /names) — into one selection; `d`'s
 * own parts win. The page then rewrites the URL to `?d=` alone. */
export function mergeSel(vals: { d?: ScanSel; date?: ScanSel; from?: ScanSel }): ScanSel | undefined {
  const { d, date, from } = vals
  // A value that names nothing wins: the page shows the miss rather than a guess.
  const invalid = d?.invalid ?? date?.invalid ?? from?.invalid
  if (invalid) return { invalid }
  return sel(d?.d ?? date?.d, d?.span, d?.from ?? (d?.span ? undefined : from?.from))
}

/** The legacy-key decoders `mergeSel` folds: each reads an ISO or compact id. */
export const legacyDateParam = (now = new Date()) => ({
  encode: (): undefined => undefined,
  decode: (v: string | undefined): ScanSel | undefined => v ? sel(decodeScan(v, now)) ?? { invalid: v } : undefined,
})
export const legacyFromParam = (now = new Date()) => ({
  encode: (): undefined => undefined,
  decode: (v: string | undefined): ScanSel | undefined => v ? sel(undefined, undefined, decodeScan(v, now)) ?? { invalid: v } : undefined,
})

/** A selection's "after" scan, resolved: the latest scan matching `d`, or the
 * latest scan when `d` is absent; null when `d` matches nothing, the value is
 * `invalid`, or `scans` is empty — never a fallback to another scan. */
export function resolveAfter(s: ScanSel | undefined, scans: readonly string[]): string | null {
  if (s?.invalid) return null
  if (s?.d) return latestScan(s.d, scans)
  let best: string | null = null
  for (const x of scans) if (best === null || x > best) best = x
  return best
}

/** A selection's "before" scan, given its resolved "after": a pinned `from`
 * resolves (latest match) among the scans before `after` — no match is null,
 * a miss, never the nearest; a span (a duration) resolves to the scan nearest
 * `after − span`; neither = undefined (the caller's default, usually the
 * previous scan, or none). */
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
 * or undefined when it selects nothing (absent); an unparseable value stays
 * verbatim (a miss). What a
 * page's own URL canonicalizes to, so OG/share views key by the same string. */
export const canonicalSel = (sp: URLSearchParams, now = new Date()): string | undefined => encodeSel(selOf(sp, now))

/** The slug a missed selection names, for a "no scan matches" message: the
 * `invalid` value, or the canonical end slug. */
export const selSlug = (s: ScanSel): string => s.invalid ?? encodeScan(s.d) ?? ''
