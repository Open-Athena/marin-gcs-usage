import { Explain } from './Help'
import { TimeSeries } from '@disk-tree/react'
import type { Annotation, Series as TsSeries } from '@disk-tree/react'
import { DEFAULT_PALETTE } from '@rdub/treemap'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { shortName } from './UserChip'
import { useUnits } from './units'
import { Skeleton } from './Busy'
import { stackSeries, youngestGenesis } from './series'
import type { Band } from './series'

// Stored bytes over the historical scans, scoped exactly like the map: the
// drilled prefix, a user, or an owner pool (`/api/series` — one row read per
// scan in that scan's index tiers; specs/view-serving.md §1). Nothing is
// precomputed per prefix and nothing is floored.

interface Pt { x: number; y: number; y0?: number }
interface Series { path: string; points: { date: string; b: number; o: number }[]; roots?: { path: string; points: { date: string; b: number; o: number }[] }[] }

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
type Layout = 'stacked' | 'lines'

// Per-root layout at the store root (`?ln` — lines on): stacked bands (the
// composition; a root's band starts at its genesis) or overlaid lines (each
// root's own movement).
function LayoutToggle({ v, set }: { v: Layout; set: (l: Layout) => void }) {
  return (
    <span className="gran" role="radiogroup" aria-label="Roots layout">
      <span className="lbl">roots</span>
      {(['stacked', 'lines'] as Layout[]).map(l => (
        <Explain key={l} text={l === 'stacked' ? 'One band per bucket, stacked to the total; a bucket’s band starts at the first scan that covered it' : 'One line per bucket, plus the total'}>
          <button role="radio" aria-checked={v === l} className={v === l ? 'on' : ''} onClick={() => set(l)}>{l}</button>
        </Explain>
      ))}
    </span>
  )
}

// y-axis origin toggle (`?y0` — from-zero on): fit the data (default — a ~1%
// wiggle on 3 PiB is invisible from zero) or anchor at zero (honest
// proportions).
function YFromToggle({ v, set }: { v: YFrom; set: (y: YFrom) => void }) {
  return (
    <span className="gran" role="radiogroup" aria-label="Y-axis range">
      <span className="lbl">y-axis</span>
      {(['data', 'zero'] as YFrom[]).map(y => (
        <Explain key={y} text={y === 'data' ? 'Fit the y-range to the data (a small movement in a large total stays visible)' : 'Start the y-axis at zero (honest proportions)'}>
          <button role="radio" aria-checked={v === y} className={v === y ? 'on' : ''} onClick={() => set(y)}>{y === 'data' ? 'fit' : 'from 0'}</button>
        </Explain>
      ))}
    </span>
  )
}

// Points sit at UTC midnight of each scan's calendar date, so labels format
// in UTC too — a local-time render shows the 8/23 scan as “Aug 22” in the US.
const dateOfX = (x: number) => new Date(x).toISOString().slice(0, 10)
const fmtX = (x: number) => new Date(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
const xOfScan = (d: string) => new Date(d.slice(0, 10)).getTime()

export function SizeOverTime({ scans, prefix, user, pool, onPickDate, onBrush, window: win, scopeLabel = 'all buckets', paths, filterLabel }: {
  /** The store's root scope word for the unscoped subtitle (`all buckets`, `the whole bucket`). */
  scopeLabel?: string
  /** The page filter's match roots: the series is their sum per scan. */
  paths?: string[]
  /** The filter text, for the subtitle. */
  filterLabel?: string
  scans: string[]
  prefix: string
  /** The owner axis's user: their bytes under `prefix`, per scan. */
  user?: string | null
  /** The owner axis's pool — `unowned` = bytes no person owns, `owned`
   * = bytes some person owns — under `prefix`, per scan. `user` wins. */
  pool?: 'unowned' | 'owned' | null
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
  const [linesP, setLinesP] = useUrlState('ln', boolParam)
  const layout: Layout = linesP ? 'lines' : 'stacked'
  const setLayout = (l: Layout) => setLinesP(l === 'lines')

  // The store root, unscoped: one trace per root (specs/root-geneses.md §2).
  const split = !prefix && !user && !pool && !paths?.length && !filterLabel
  const scope = (user ? `&lens=user:${encodeURIComponent(user)}` : pool ? `&o=${pool}` : '') + (paths?.length ? `&paths=${encodeURIComponent(paths.join(','))}` : '') + (split ? '&split=roots' : '')
  const seriesQ = useQuery<Series>({
    queryKey: ['series', prefix, scope, scans.length],
    // Under a filter, wait for its match roots: the whole-store series is not
    // what the page asked for.
    enabled: scans.length > 1 && !(filterLabel && !paths?.length),
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const r = await fetch(`/api/series?path=${encodeURIComponent(prefix)}${scope}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`series: ${r.status}`)
      return r.json()
    },
  })

  const label = user ? shortName(user) : pool ?? (prefix || 'total')
  const toPts = (points: { date: string; b: number }[]): Pt[] => points.map(p => ({ x: xOfScan(p.date), y: p.b })).sort((a, b) => a.x - b.x)
  const total = useMemo(() => toPts(seriesQ.data?.points ?? []), [seriesQ.data])
  // The roots' traces (split mode), keyed by bucket, coloured by slot (largest
  // at the latest scan = slot 0, as the map colours the root's children).
  const roots = useMemo(() => {
    const rs = seriesQ.data?.roots ?? []
    if (rs.length < 2) return []
    const latest = (r: { points: { date: string; b: number }[] }) => r.points[r.points.length - 1]?.b ?? 0
    const bySize = [...rs].sort((a, b) => latest(b) - latest(a))
    const slot = new Map(bySize.map((r, i) => [r.path, i]))
    return rs.map(r => ({ key: r.path, color: DEFAULT_PALETTE[slot.get(r.path)! % DEFAULT_PALETTE.length], points: toPts(r.points) }))
  }, [seriesQ.data])
  // The x before which the total lacks a root — the total is dashed there.
  const genesis = useMemo(() => youngestGenesis(roots), [roots])
  const series = useMemo((): TsSeries<Pt>[] => {
    if (total.length < 2) return []
    if (!roots.length) return [{ key: 'scoped', label, color: 'var(--s1)', points: total }]
    if (layout === 'stacked') {
      // Bands stack up to the total; the total rides along the stack's top
      // edge as a thin grey line (dashed before the youngest genesis) so the
      // tooltip lists it and the pre-genesis stretch reads as incomplete.
      // `y0: 0` keeps its tooltip value the whole total, not a band height.
      const bands: TsSeries<Pt>[] = stackSeries(roots).map((s, i) => ({ key: s.key, label: s.key, color: roots[i].color, points: s.points as Band[] }))
      return [...bands, { key: 'total', label: 'total', color: 'var(--ink-3)', area: false, points: total.map(p => ({ ...p, y0: 0 })), dashBeforeX: genesis ?? undefined }]
    }
    return [...roots.map(r => ({ key: r.key, label: r.key, color: r.color, area: false, points: r.points })), { key: 'total', label: 'total', color: 'var(--s1)', area: false, points: total, dashBeforeX: genesis ?? undefined }]
  }, [total, roots, layout, label, genesis])
  const stacked = roots.length > 0 && layout === 'stacked'
  // A scope that owns nothing here in any scan is a flat zero line — say so
  // instead of drawing an empty axis.
  const allZero = series.length === 1 && series[0].points.every(p => p.y === 0)

  // Callouts at the points a reader looks for first: the ends of the series
  // and its extremes. Coinciding roles (first is also max) share one label.
  // With roots they annotate the total.
  const annotations = useMemo((): Annotation[] => {
    const pts = roots.length ? total : series.length === 1 ? series[0].points : []
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

  const firstX = total[0]?.x
  if (scans.length < 2) return null
  return (
    <section id="over-time">
      <h2>Size over time <YFromToggle v={yFrom} set={setYFrom} />{roots.length > 0 && <LayoutToggle v={layout} set={setLayout} />}</h2>
      <p className="sub">
        {user
          ? <><b>{shortName(user)}</b>’s bytes{prefix ? <> under <code>{prefix}</code></> : ''} per scan.</>
          : pool === 'unowned'
            ? <>Bytes no person owns{prefix ? <> under <code>{prefix}</code></> : ''}, per scan.</>
            : pool === 'owned'
              ? <>Bytes owned by a person{prefix ? <> under <code>{prefix}</code></> : ''}, per scan.</>
              : paths?.length
                ? <>Stored bytes under “{filterLabel}” ({paths.length} {paths.length === 1 ? 'prefix' : 'prefixes'}) per scan.</>
              : prefix
                ? <>Stored bytes under <code>{prefix}</code> per scan.</>
                : roots.length
                  ? <>Stored bytes per scan, one {stacked ? 'band' : 'line'} per bucket{genesis != null && <> — the total is dashed before every bucket was in the scan</>}.</>
                  : <>Total stored bytes per scan ({scopeLabel}).</>}
        {' '}Each point is that scan’s own index row — exact, at any depth.
        {seriesQ.isError && <> <i>(series unavailable)</i></>}
      </p>
      {allZero ? (
        <p className="loading">
          {user ? <><b>{shortName(user)}</b> owns nothing{prefix ? <> under <code>{prefix}</code></> : ''} in any scan</>
            : pool === 'unowned' ? <>nothing{prefix ? <> under <code>{prefix}</code></> : ''} is unowned in any scan</>
              : <>nothing{prefix ? <> under <code>{prefix}</code></> : ''} in any scan</>}
        </p>
      ) : series.length > 0 ? (
        <TimeSeries<Pt>
          series={series}
          getX={p => p.x}
          getY={p => p.y}
          getY0={stacked ? p => p.y0 ?? 0 : undefined}
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
      ) : (
        seriesQ.isLoading ? <Skeleton height={220} label="loading series…" /> : <p className="loading">fewer than two scans hold this path</p>
      )}
      {roots.length > 0 && (
        <div className="legend roots-legend">
          {roots.map(r => (
            <span className="li" key={r.key}>
              <span className="sw" style={{ background: r.color }} />
              {r.key}
              {r.points[0] && firstX != null && r.points[0].x > firstX && <span className="since"> · since {fmtX(r.points[0].x)}</span>}
            </span>
          ))}
        </div>
      )}
    </section>
  )
}
