import { TimeSeries } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import type { Meta } from './types'
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
const Y_KEY = 'gcs-usage:sot-y'
const loadYFrom = (): YFrom => {
  try { if (localStorage.getItem(Y_KEY) === 'zero') return 'zero' } catch { /* no storage */ }
  return 'data'
}

// y-axis origin toggle: fit the data (default — a ~1% wiggle on 3 PiB is
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

export function SizeOverTime({ scans, prefix, base, fate = false }: {
  scans: string[]
  prefix: string
  base: string
  /** Mark-progress mode (the To-do lens): plot keep/sweep/undecided per scan
   * from the replayed ledger instead of stored-bytes. Renders nothing until
   * the index carries `fate`. */
  fate?: boolean
}) {
  const { fmtBytes, units } = useUnits()
  const [yFrom, setYFromState] = useState<YFrom>(loadYFrom)
  const setYFrom = (y: YFrom) => { setYFromState(y); try { localStorage.setItem(Y_KEY, y) } catch { /* in-memory only */ } }

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
  const needFleet = !scopedArr
  const metas = useQuery({
    queryKey: ['size-series', base, scans],
    enabled: scans.length > 1 && needFleet,
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

  const series = useMemo(() => {
    if (fate) {
      if (!idx?.fate) return []
      const mk = (k: 'keep' | 'sweep' | 'undecided', label: string, color: string) => ({
        key: k,
        label,
        color,
        points: idx.dates.map((d, i) => ({ x: new Date(d).getTime(), y: idx.fate![k][i] })).sort((a, b) => a.x - b.x),
      })
      return [
        mk('undecided', 'undecided', 'var(--ink-2)'),
        mk('keep', 'keep', 'var(--mk-keep)'),
        mk('sweep', 'sweep', 'var(--mk-del)'),
      ]
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
  }, [fate, scopedArr, idx, metas.data, prefix])

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
      <section id="size-over-time">
        <h2>Mark progress <YFromToggle v={yFrom} set={setYFrom} /></h2>
        <p className="sub">
          Keep / sweep / undecided bytes per scan — the actions ledger replayed against each archived
          scan, so the gray line is the review burn-down.
        </p>
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          formatY={fmtBytes}
          formatX={x => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          yTickValues={yTickValues}
          yFrom={yFrom}
          yLabel="bytes"
          height={220}
        />
      </section>
    )
  }
  const scoped = !!scopedArr
  const belowFloor = !!prefix && !!idx && !scopedArr
  return (
    <section id="size-over-time">
      <h2>Size over time <YFromToggle v={yFrom} set={setYFrom} /></h2>
      <p className="sub">
        {scoped
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
          formatX={x => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}
          yTickValues={yTickValues}
          yFrom={yFrom}
          yLabel="stored bytes"
          height={220}
        />
      )}
    </section>
  )
}
