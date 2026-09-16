import { TimeSeries } from '@disk-tree/react'
import type { Annotation } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { Skeleton } from './Busy'
import { scanTime } from './scan'
import { useUnits } from './units'

// Stored bytes over the historical scans, scoped like the map: the drilled
// prefix (`/api/series` — one row read per scan in that scan's index tiers;
// specs/view-serving.md §1). Nothing is precomputed per prefix and nothing is
// floored. gcs's component minus its owner axis (no attribution here); points
// sit at each scan's UTC instant (cw scans are sub-daily).

interface Pt { x: number; y: number }
interface Series { path: string; points: { date: string; b: number; o: number }[] }

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
// wiggle on 800 TiB is invisible from zero) or anchor at zero (honest
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

export function SizeOverTime({ scans, prefix, onPickDate, onBrush, window: win }: {
  scans: string[]
  /** The drilled prefix (bucket-relative index path; '' = everything). */
  prefix: string
  /** Click a point → view the page as of that scan (pins `?d=`). */
  onPickDate?: (scan: string) => void
  /** Drag across the chart → make [from, to] the page's diff window. */
  onBrush?: (from: string, to: string) => void
  /** The page's current diff window (scan ids), shaded on the chart. */
  window?: [string, string]
}) {
  const { fmtBytes, units } = useUnits()
  const [y0P, setY0P] = useUrlState('y0', boolParam)
  const yFrom: YFrom = y0P ? 'zero' : 'data'
  const setYFrom = (y: YFrom) => setY0P(y === 'zero')
  // Points are scan instants; a click or brush maps the snapped x back to its scan id.
  const scanAt = (x: number) => scans.find(d => scanTime(d) === x)
  const pickX = onPickDate && ((x: number) => { const s = scanAt(x); if (s) onPickDate(s) })
  const brushX = onBrush && ((x0: number, x1: number) => { const a = scanAt(x0); const b = scanAt(x1); if (a && b) onBrush(a, b) })

  const seriesQ = useQuery<Series>({
    queryKey: ['series', prefix, scans.length],
    enabled: scans.length > 1,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const r = await fetch(`/api/series?path=${encodeURIComponent(prefix)}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`series: ${r.status}`)
      return r.json()
    },
  })

  const series = useMemo(() => {
    const pts = (seriesQ.data?.points ?? [])
      .map(p => ({ x: scanTime(p.date), y: p.b }))
      .filter(p => Number.isFinite(p.x))
      .sort((a, b) => a.x - b.x)
    if (pts.length < 2) return []
    return [{ key: 'scoped', label: prefix || 'total', color: 'var(--s1)', points: pts }]
  }, [seriesQ.data, prefix])
  // A prefix that holds nothing in any scan is a flat zero line — say so
  // instead of drawing an empty axis.
  const allZero = series.length === 1 && series[0].points.every(p => p.y === 0)

  // Callouts at the points a reader looks for first: the ends of the series
  // and its extremes. Coinciding roles (first is also max) share one label.
  const annotations = useMemo((): Annotation[] => {
    if (series.length !== 1) return []
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
  }, [series, fmtBytes])

  const yTickValues = useMemo(() => {
    const ys = series.flatMap(s => s.points.map(p => p.y))
    const max = Math.max(0, ...ys)
    const min = yFrom === 'data' && ys.length ? Math.min(...ys) : 0
    // Fit mode pads 5% each side (TimeSeries), so tick that slightly wider range.
    const pad = yFrom === 'data' ? (max - min) * 0.05 : 0
    return unitTicks(Math.max(0, min - pad), max + pad, units === 'iec' ? 1024 : 1000)
  }, [series, units, yFrom])

  if (scans.length < 2) return null
  return (
    <section id="over-time">
      <h2>Size over time <YFromToggle v={yFrom} set={setYFrom} /></h2>
      <p className="sub">
        {prefix ? <>Stored bytes under <code>{prefix}</code> per scan.</> : <>Total stored bytes per scan (whole bucket).</>}
        {' '}Each point is that scan’s own index row — exact, at any depth.
        {onPickDate && ' Click a point to view the page as of that scan'}{onBrush && ', drag to set the diff window'}.
        {seriesQ.isError && <> <i>(series unavailable)</i></>}
      </p>
      {allZero ? (
        <p className="loading">nothing{prefix ? <> under <code>{prefix}</code></> : ''} in any scan</p>
      ) : series.length > 0 ? (
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          formatY={fmtBytes}
          formatX={x => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          yTickValues={yTickValues}
          annotations={annotations}
          yFrom={yFrom}
          yLabel="stored bytes"
          height={220}
          onPickX={pickX}
          onBrush={brushX}
          window={win && [scanTime(win[0]), scanTime(win[1])]}
        />
      ) : (
        seriesQ.isLoading ? <Skeleton height={220} label="loading series…" /> : <p className="loading">fewer than two scans hold this path</p>
      )}
    </section>
  )
}
