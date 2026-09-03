import { useEffect, useMemo, useState } from 'react'
import { useUrlState } from 'use-prms'

// How often an unpinned tab re-checks for newly published scans.
export const SCANS_POLL_MS = 5 * 60_000

// Scan labels: drop the redundant year for the current one, so a list of
// same-year scans reads as `8/17` rather than `2026-08-17`. Scan ids are
// `YYYY-MM-DD`, optionally sub-daily as `YYYY-MM-DDTHHMM`.
export function fmtScan(s: string, now = new Date()): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):?(\d{2}))?/.exec(s)
  if (!m) return s
  const [, y, mo, d, hh, mm] = m
  if (!hh) {
    // Date-only ids (the daily GCS job) are calendar dates, not instants —
    // rendering them through a timezone would shift some readers a day off.
    return Number(y) === now.getFullYear() ? `${Number(mo)}/${Number(d)}` : `${y}-${mo}-${d}`
  }
  // Sub-daily ids are UTC instants; display in the viewer's local time,
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

// `?d` is a *prefix* of a scan id (always UTC), accepted in several spellings —
// examples that all pin the 2026-08-19T1008 scan:
//   260819-1008 · 260819T10 (compact date; `-` or `T` before the time)
//   8-19-10 · 8-19-10-08    (M-D-H[-MM]; current year assumed)
//   26-8-19-10 · 2026-8-19-10 (year-first when the lead component can't be a month)
// The canonical/emitted form stays compact (`260819-1008`). A token matching
// several scans renders the newest plus a disambiguation strip listing the rest.
export const encodeScan = (v: string | undefined): string | undefined => {
  const m = v && /^\d{2}(\d{2})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(v)
  if (!m) return v || undefined
  const [, y, mo, d, hh, mm] = m
  return `${y}${mo}${d}` + (hh ? `-${hh}${mm ?? ''}` : '')
}

export const decodeScan = (e: string | undefined, now = new Date()): string | undefined => {
  if (!e) return undefined
  const pad = (n: string) => n.padStart(2, '0')
  const compact = /^(\d{2})(\d{2})(\d{2})(?:[T-](\d{2})(\d{2})?)?$/.exec(e)
  if (compact) {
    const [, y, mo, d, hh, mm] = compact
    return `20${y}-${mo}-${d}` + (hh ? `T${hh}${mm ?? ''}` : '')
  }
  const parts = e.split(/[T-]/)
  if (parts.length < 2 || parts.length > 5 || parts.some(p => !/^(\d{1,2}|\d{4})$/.test(p))) return undefined
  // A lead component that can't be a month is a year (2- or 4-digit); 4-digit
  // components anywhere else are malformed.
  const yearFirst = parts[0].length === 4 || Number(parts[0]) > 12
  if (parts.slice(yearFirst ? 1 : 0).some(p => p.length > 2)) return undefined
  const y = yearFirst ? (parts[0].length === 4 ? parts[0] : `20${parts[0]}`) : String(now.getUTCFullYear())
  const [mo, d, hh, mm] = parts.slice(yearFirst ? 1 : 0)
  if (!mo || !d || Number(mo) < 1 || Number(mo) > 12 || Number(d) < 1 || Number(d) > 31) return undefined
  if ((hh && Number(hh) > 23) || (mm && Number(mm) > 59)) return undefined
  return `${y}-${pad(mo)}-${pad(d)}` + (hh ? `T${pad(hh)}${mm ? pad(mm) : ''}` : '')
}

export interface Scan {
  asof: string | null
  scans: string[]
  dMatches: string[]
  dP: string | undefined
  setDP: (v: string | undefined) => void
}

// Shared scan resolution: `?d=YYMMDD` (a prefix of a scan id) pins a scan;
// absent means "latest" (a first-class state, so a parked tab follows new scans
// via the poll rather than freezing on the day it opened). `scans` is
// newest-first, so the first prefix match is the newest. Ported from gcs
// (`9b8d237`, specs/scan-param-all-pages.md) minus react-query — this branch
// fetches with plain `fetch`, so the poll is a bare interval.
export function useScan(): Scan {
  const [dP, setDP] = useUrlState('d', { encode: encodeScan, decode: decodeScan })
  const [scans, setScans] = useState<string[]>([])
  useEffect(() => {
    // Per-scan payloads are immutable once published; only the list moves.
    const load = () => void fetch('/data/scans.json').then(r => (r.ok ? r.json() : [])).then(setScans).catch(() => {})
    load()
    const t = setInterval(load, SCANS_POLL_MS)
    return () => clearInterval(t)
  }, [])
  const dMatches = useMemo(() => (dP ? scans.filter(s => s.startsWith(dP)) : []), [dP, scans])
  const asof = dMatches[0] ?? scans[0] ?? null
  return { asof, scans, dMatches, dP, setDP }
}
