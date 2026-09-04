import { TimeSeries } from '@disk-tree/react'
import type { Annotation } from '@disk-tree/react'
import { useEffect, useMemo, useState } from 'react'
import { scanTime } from './scan'
import type { Meta } from './types'
import { useUnits } from './units'

// Stored bytes over the historical scans (CP'd from gcs; specs/size-over-time.md
// case 1, fleet-total form). gcs scopes this to the drilled prefix via a
// precomputed cross-scan `series.json`; the CW job publishes no such index yet,
// so this plots the bucket total from each scan's meta.json (~4 KB each,
// fetched once and cached for the tab's life — scans are immutable).

interface Pt { x: number; y: number }

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
const Y_KEY = 'gcs-usage:sot-y'
const loadYFrom = (): YFrom => {
  try { if (localStorage.getItem(Y_KEY) === 'zero') return 'zero' } catch { /* no storage */ }
  return 'data'
}

// y-axis origin toggle: fit the data (default — a ~1% wiggle on 800 TiB is
// invisible from zero) or anchor at zero (honest proportions).
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

const metaLoads = new Map<string, Promise<Meta | null>>()
const loadMeta = (d: string): Promise<Meta | null> => {
  let p = metaLoads.get(d)
  if (!p) {
    p = fetch(`/data/${d}/meta.json`).then(r => (r.ok ? (r.json() as Promise<Meta>) : null)).catch(() => null)
    metaLoads.set(d, p)
  }
  return p
}

export function SizeOverTime({ scans }: { scans: string[] }) {
  const { fmtBytes, units } = useUnits()
  const [yFrom, setYFromState] = useState<YFrom>(loadYFrom)
  const setYFrom = (y: YFrom) => { setYFromState(y); try { localStorage.setItem(Y_KEY, y) } catch { /* in-memory only */ } }

  const [totals, setTotals] = useState<Record<string, number>>({})
  useEffect(() => {
    let live = true
    for (const d of scans) {
      if (d in totals) continue
      void loadMeta(d).then(m => { if (live && m) setTotals(prev => (d in prev ? prev : { ...prev, [d]: m.total_bytes })) })
    }
    return () => { live = false }
  }, [scans, totals])

  const series = useMemo(() => {
    const points = scans
      .map(d => ({ x: scanTime(d), y: totals[d] }))
      .filter((p): p is Pt => p.y != null && Number.isFinite(p.x))
      .sort((a, b) => a.x - b.x)
    return points.length < 2 ? [] : [{ key: 'total', label: 'total', color: 'var(--s1)', points }]
  }, [scans, totals])

  // Callouts at the points a reader looks for first: the ends of the series
  // and its extremes. Coinciding roles (first is also max) share one label.
  const annotations = useMemo((): Annotation[] => {
    const pts = series[0]?.points ?? []
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
    <section id="size-over-time">
      <h2>Size over time <YFromToggle v={yFrom} set={setYFrom} /></h2>
      <p className="sub">Total stored bytes per scan (whole bucket).</p>
      {series.length > 0 && (
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
        />
      )}
    </section>
  )
}
