import { TimeSeries } from '@disk-tree/react'
import type { Annotation } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import type { FateAxis } from './sweep'
import type { Meta } from './types'
import { shortName } from './UserChip'
import { useUnits } from './units'

// Stored bytes over the historical scans (specs/size-over-time.md, case 1),
// scoped to the currently-drilled prefix. The cross-scan index `series.json`
// (precomputed by `gcs-usage series`) carries per-prefix bytes per date for
// every prefix above a fold floor; the client just looks up the current `?p=`.
// Below-floor / not-yet-published → fall back to the fleet total from meta.json.

interface Pt { x: number; y: number }
interface SeriesIndex {
  dates: string[]
  prefixes: string[]
  bytes: Record<string, (number | null)[]>
  /** Ledger replayed per scan date (gcs-usage series -a) — the burn-down. */
  fate?: Record<'keep' | 'sweep' | 'undecided', number[]>
}

// Nice y-ticks aligned to the *display* unit: a base-10-nice byte value (1e15)
// is an ugly binary label (909 TiB), so nice-tick in the unit's own base
// (1024 for IEC → 1024/2048/3072 TiB; 1000 for SI → round TB/PB).
// `min` > 0 = a fitted axis: ticks cover [min, max] at the same unit-nice step.
const unitTicks = (min: number, max: number, base: number, count = 4): number[] => {
  if (max <= 0) return [0]
  const span = Math.max(max - min, max * 1e-6)
  const scale = base ** Math.floor(Math.log(max) / Math.log(base))
  const rawStep = span / scale / count
  const mag = 10 ** Math.floor(Math.log10(rawStep))
  const norm = rawStep / mag
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag * scale
  const out: number[] = []
  for (let v = Math.ceil(min / step) * step; v <= max + step / 100; v += step) out.push(v)
  return out
}

type YFrom = 'data' | 'zero'

// y-axis origin toggle (`?y0` — from-zero on): fit the data (default — a ~1%
// wiggle on 3 PiB is invisible from zero) or anchor at zero (honest
// proportions).
function YFromToggle({ v, set }: { v: YFrom; set: (y: YFrom) => void }) {
  return (
    <span className="gran" role="radiogroup" aria-label="Y-axis range">
      <span className="lbl">y-axis</span>
      {(['data', 'zero'] as YFrom[]).map(y => (
        <button key={y} role="radio" aria-checked={v === y} className={v === y ? 'on' : ''} onClick={() => set(y)} title={y === 'data' ? 'Fit the y-range to the data' : 'Start the y-axis at zero'}>
          {y === 'data' ? 'fit' : 'from 0'}
        </button>
      ))}
    </span>
  )
}

// Points sit at UTC midnight of each scan's calendar date, so labels format
// in UTC too — a local-time render shows the 8/23 scan as “Aug 22” in the US.
const dateOfX = (x: number) => new Date(x).toISOString().slice(0, 10)
const fmtX = (x: number) => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
const xOfScan = (d: string) => new Date(d.slice(0, 10)).getTime()

export function SizeOverTime({ scans, prefix, base, fates, user, pool, onPickDate, onBrush, window: win }: {
  scans: string[]
  prefix: string
  base: string
  /** Mark-progress mode (the page's mark axis is active): plot the selected
   * keep/sweep/undecided lines per scan from the replayed ledger instead of
   * stored bytes. Renders nothing until the index carries `fate`. */
  fates?: ReadonlySet<FateAxis> | null
  /** The owner axis's user: plot THEIR attributed bytes per scan (from each
   * scan's meta.json — estate-wide; per-user series aren't prefix-scoped). */
  user?: string | null
  /** The owner axis's pool — `unclaimed` = total − Σ user bytes (the
   * nobody-owns-it pool), `claimed` = Σ user bytes. `user` wins. */
  pool?: 'unclaimed' | 'claimed' | null
  /** Click a point → view the page as of that scan (pins `?d=`). */
  onPickDate?: (date: string) => void
  /** Drag across the chart → make [from, to] the page's diff window. */
  onBrush?: (from: string, to: string) => void
  /** The page's current diff window (scan ids), shaded on the chart. */
  window?: [string, string]
}) {
  const { fmtBytes, units } = useUnits()
  const [y0P, setY0P] = useUrlState('y0', boolParam)
  const yFrom: YFrom = y0P ? 'zero' : 'data'
  const setYFrom = (y: YFrom) => setY0P(y === 'zero')

  // The cross-scan index (one small file); optional — 404 until it's published.
  const indexQ = useQuery<SeriesIndex | null>({
    queryKey: ['size-index', base],
    staleTime: Infinity,
    retry: false,
    queryFn: async () => {
      const r = await fetch(`${base}/series.json`)
      return r.ok ? (r.json() as Promise<SeriesIndex>) : null
    },
  })

  // Fleet-total fallback: per-date meta.json (~4 KB each) — only fetched/used
  // when the index is missing or the drilled prefix isn't in it.
  const idx = indexQ.data ?? null
  const scopedArr = prefix && idx?.bytes[prefix] ? idx.bytes[prefix] : null
  const slice = user ? { kind: 'user' as const, key: user } : pool ? { kind: 'pool' as const, key: pool } : null
  const needFleet = !scopedArr
  const metas = useQuery({
    queryKey: ['size-series', base, scans],
    enabled: scans.length > 1 && (needFleet || slice != null),
    staleTime: Infinity,
    queryFn: async () => {
      const rows = await Promise.all(scans.map(async d => {
        const r = await fetch(`${base}/${d}/meta.json`)
        if (!r.ok) return null
        return { date: d, m: (await r.json()) as Meta }
      }))
      return rows.filter((r): r is { date: string; m: Meta } => r != null)
    },
  })

  // Mark-progress only when the index carries the replayed ledger; until it
  // does (it's optional), an active mark axis falls back to the byte series.
  const fate = !!fates && idx?.fate != null
  const fateKey = fate && fates ? [...fates].sort().join(',') : ''
  const series = useMemo(() => {
    if (fate && fates) {
      if (!idx?.fate) return []
      const mk = (k: 'keep' | 'sweep' | 'undecided', label: string, color: string) => ({
        key: k,
        label,
        color,
        points: idx.dates.map((d, i) => ({ x: new Date(d).getTime(), y: idx.fate![k][i] })).sort((a, b) => a.x - b.x),
      })
      return [
        ...(fates.has('unmarked') ? [mk('undecided', 'undecided', 'var(--ink-2)')] : []),
        ...(fates.has('keep') ? [mk('keep', 'keep', 'var(--mk-keep)')] : []),
        ...(fates.has('sweep') ? [mk('sweep', 'sweep', 'var(--mk-del)')] : []),
      ]
    }
    if (slice) {
      // Per-user / pool series come from each scan's meta.json (older scans
      // predate attribution → no point rather than a fake zero).
      const points = (metas.data ?? [])
        .map(({ date, m }) => {
          const claimed = m.users ? m.users.reduce((s, u) => s + u.b, 0) : null
          const y = slice.kind === 'user'
            ? m.users?.find(u => u.u === slice.key)?.b ?? null
            : slice.key === 'unclaimed'
              ? (claimed == null ? null : m.total_bytes - claimed)
              : claimed
          return y == null ? null : { x: new Date(date).getTime(), y }
        })
        .filter((p): p is Pt => p != null)
        .sort((a, b) => a.x - b.x)
      if (points.length < 2) return []
      return [{ key: `${slice.kind}:${slice.key}`, label: slice.kind === 'user' ? shortName(slice.key) : slice.key, color: 'var(--s1)', points }]
    }
    if (scopedArr && idx) {
      const points = idx.dates
        .map((d, i) => ({ x: new Date(d).getTime(), y: scopedArr[i] }))
        .filter((p): p is Pt => p.y != null)
        .sort((a, b) => a.x - b.x)
      return [{ key: 'scoped', label: prefix, color: 'var(--s1)', points }]
    }
    const rows = metas.data ?? []
    if (rows.length < 2) return []
    return [{
      key: 'total',
      label: 'total',
      color: 'var(--s1)',
      points: rows.map(r => ({ x: new Date(r.date).getTime(), y: r.m.total_bytes })).sort((a, b) => a.x - b.x),
    }]
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fateKey, slice?.kind, slice?.key, scopedArr, idx, metas.data, prefix])

  // Callouts at the points a reader looks for first: the ends of the series
  // and its extremes. Coinciding roles (first is also max) share one label.
  // Single-series views only (the fate burn-down would triple-label).
  const annotations = useMemo((): Annotation[] => {
    if (fate || series.length !== 1) return []
    const pts = series[0].points
    if (pts.length < 2) return []
    let lo = pts[0]
    let hi = pts[0]
    for (const p of pts) {
      if (p.y < lo.y) lo = p
      if (p.y > hi.y) hi = p
    }
    const picks = new Map<Pt, boolean>() // point → below?
    picks.set(hi, false)
    picks.set(lo, true)
    for (const p of [pts[0], pts[pts.length - 1]]) if (!picks.has(p)) picks.set(p, p.y < (lo.y + hi.y) / 2)
    return [...picks].map(([p, below]) => ({ x: p.x, y: p.y, label: fmtBytes(p.y), below }))
  }, [fate, series, fmtBytes])

  const yTickValues = useMemo(() => {
    const ys = series.flatMap(s => s.points.map(p => p.y))
    const max = Math.max(0, ...ys)
    const min = yFrom === 'data' && ys.length ? Math.min(...ys) : 0
    // Fit mode pads 5% each side (TimeSeries), so tick that slightly wider range.
    const pad = yFrom === 'data' ? (max - min) * 0.05 : 0
    return unitTicks(Math.max(0, min - pad), max + pad, units === 'iec' ? 1024 : 1000)
  }, [series, units, yFrom])

  if (scans.length < 2) return null
  if (fate) {
    if (!series.length) return null
    return (
      <section id="over-time">
        <h2>Mark progress <YFromToggle v={yFrom} set={setYFrom} /></h2>
        <p className="sub">
          {series.map(s => s.label).join(' / ')} bytes per scan, whole estate — the actions ledger replayed
          against each archived scan{fates?.has('unmarked') ? ', so the gray line is the review burn-down' : ''}.
        </p>
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          formatY={fmtBytes}
          formatX={fmtX}
          yTickValues={yTickValues}
          yFrom={yFrom}
          yLabel="bytes"
          height={220}
          onPickX={onPickDate && (x => onPickDate(dateOfX(x)))}
          onBrush={onBrush && ((x0, x1) => onBrush(dateOfX(x0), dateOfX(x1)))}
          window={win && [xOfScan(win[0]), xOfScan(win[1])]}
        />
      </section>
    )
  }
  const scoped = !!scopedArr
  const belowFloor = !!prefix && !!idx && !scopedArr
  return (
    <section id="over-time">
      <h2>Size over time <YFromToggle v={yFrom} set={setYFrom} /></h2>
      <p className="sub">
        {slice
          ? slice.kind === 'user'
            ? <><b>{shortName(slice.key)}</b>’s attributed bytes per scan — whole estate (per-user series aren’t prefix-scoped).</>
            : slice.key === 'unclaimed'
              ? <>Bytes no person owns, per scan — whole estate.</>
              : <>Bytes attributed to a person, per scan — whole estate.</>
          : scoped
          ? <>Stored bytes under <code>{prefix}</code> per scan — the drilled subtree, from the cross-scan index.</>
          : <>Total stored bytes per scan (fleet-wide).{' '}
              {belowFloor
                ? <><code>{prefix}</code> is below the size-index floor, so no scoped series — showing the fleet total.</>
                : <>Drill in to scope this to a subpath.</>}</>}
      </p>
      {indexQ.isError && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load the size index.</p>}
      {series.length > 0 && (
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          formatY={fmtBytes}
          formatX={fmtX}
          yTickValues={yTickValues}
          yFrom={yFrom}
          yLabel="stored bytes"
          height={220}
          annotations={annotations}
          onPickX={onPickDate && (x => onPickDate(dateOfX(x)))}
          onBrush={onBrush && ((x0, x1) => onBrush(dateOfX(x0), dateOfX(x1)))}
          window={win && [xOfScan(win[0]), xOfScan(win[1])]}
        />
      )}
    </section>
  )
}
