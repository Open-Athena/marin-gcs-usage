import { useMemo, useState } from 'react'
import { dateColor, dateGradientCss, userColor } from './colors'
import type { UserIndex } from './colors'
import type { AgeRow, ColorMode, Granularity } from './types'
import { useUnits } from './units'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']

const dateBarColor = (i: number, n: number): string => dateColor(n > 1 ? i / (n - 1) : 1)

const dayToDate = (d: number): Date => new Date(d * 86400_000)
const iso = (d: number): string => dayToDate(d).toISOString().slice(0, 10)
const isoMonth = (d: number): string => iso(d).slice(0, 7)

// bucket start (epoch days) for a row's day under the given granularity
const bucketOf = (d: number, gran: Granularity): number => {
  if (gran === 'day') return d
  if (gran === 'week') return d - ((d + 3) % 7) // epoch day 0 = Thu; Monday start
  const dt = dayToDate(d)
  return Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), 1) / 86400_000
}

const bucketLabel = (b: number, gran: Granularity): string =>
  gran === 'month' ? isoMonth(b) : iso(b)

/** Stacked bars of bytes by created date (month/week/day), split per color mode. */
export function AgeChart({ rows, catOrder, mode, userIdx }: {
  rows: AgeRow[]
  catOrder: string[]
  mode: ColorMode
  userIdx: UserIndex
}) {
  const { fmtBytes } = useUnits()
  // Default to whichever granularity yields a readable number of bars. A fixed
  // 'month' default is wrong at both ends: CoreWeave's objects are all from the
  // last ~8 weeks (3 fat bars, no shape), while GCS spans years (unreadable at
  // /day). Picking by bar count gets /day for the former and /month for the
  // latter with no per-store special-casing.
  const [gran, setGran] = useState<Granularity>(() => {
    const days = rows.map(r => r.d).filter(Number.isFinite)
    if (!days.length) return 'month'
    const count = (g: Granularity) => new Set(days.map(d => bucketOf(d, g))).size
    const TARGET = 30
    return (['day', 'week', 'month'] as Granularity[])
      .reduce((a, b) => (Math.abs(count(b) - TARGET) < Math.abs(count(a) - TARGET) ? b : a))
  })
  const [hover, setHover] = useState<{ b: number; x: number; y: number } | null>(null)

  const { buckets, byBucket, colorOf, legend } = useMemo(() => {
    const slotMap = new Map(catOrder.slice(0, 8).map((k, i) => [k, SLOTS[i]]))
    const userMode = mode === 'user'
    const keyOf = (r: AgeRow) =>
      userMode ? (r.u ?? 'unattributed')
      : slotMap.has(r.d1) ? r.d1 : '(other)'
    const byBucket = new Map<number, Map<string, number>>()
    for (const r of rows) {
      if (!Number.isFinite(r.d)) continue // pre-day-granularity snapshot rows
      const bk = bucketOf(r.d, gran)
      const k = keyOf(r)
      const m = byBucket.get(bk) ?? new Map<string, number>()
      m.set(k, (m.get(k) ?? 0) + r.b)
      byBucket.set(bk, m)
    }
    const buckets = [...byBucket.keys()].sort((a, b) => a - b)
    const colorOf = (k: string): string =>
      userMode ? (k === 'unattributed' ? 'var(--t-unattr)' : userColor(k, userIdx))
      : `var(${slotMap.get(k) ?? '--other'})`
    const legend: [string, string][] =
      userMode
        ? [
            ...[...userIdx.keys()].slice(0, 10).map((u): [string, string] => [u, userColor(u, userIdx)]),
            ['unattributed', 'var(--t-unattr)'],
          ]
        : [
            ...catOrder.slice(0, 8).map((k, i): [string, string] => [k, `var(${SLOTS[i]})`]),
            ['(other)', 'var(--other)'],
          ]
    return { buckets, byBucket, colorOf, legend }
  }, [rows, catOrder, mode, userIdx, gran])

  const maxB = useMemo(
    () => Math.max(...buckets.map(b => [...byBucket.get(b)!.values()].reduce((a, v) => a + v, 0))),
    [buckets, byBucket],
  )

  if (buckets.length === 0) return null

  const W = 900
  const H = 220
  const bw = W / Math.max(buckets.length, 1)
  const gap = bw > 4 ? 1 : bw > 1.5 ? 0.4 : 0
  const tickEvery = Math.ceil(buckets.length / 12)

  return (
    <div className="agechart">
      <div className="legend">
        {mode === 'date' ? (
          <span className="li gradli">
            older
            <span className="gradbar" style={{ background: dateGradientCss() }} />
            newer
          </span>
        ) : (
          legend.map(([k, v]) => (
            <span className="li" key={k}>
              <span className="sw" style={{ background: v }} />
              {k}
            </span>
          ))
        )}
        <span className="gran" role="radiogroup" aria-label="Time granularity">
          {(['month', 'week', 'day'] as Granularity[]).map(g => (
            <button key={g} role="radio" aria-checked={gran === g} className={gran === g ? 'on' : ''} onClick={() => setGran(g)}>
              {g}
            </button>
          ))}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H + 24}`} preserveAspectRatio="none" role="img" aria-label={`Bytes by created ${gran}`}>
        {buckets.map((bk, i) => {
          const parts = byBucket.get(bk)!
          let y = H
          const segs = [...parts.entries()].sort((a, b) => b[1] - a[1]).map(([k, b]) => {
            const h = (b / maxB) * H
            y -= h
            return <rect key={k} x={i * bw + gap} y={y} width={Math.max(bw - 2 * gap, 0.8)} height={Math.max(h - gap, 0)} fill={mode === 'date' ? dateBarColor(i, buckets.length) : colorOf(k)} rx={bw > 4 ? 1.5 : 0} />
          })
          return (
            <g
              key={bk}
              onMouseMove={e => setHover({ b: bk, x: e.clientX, y: e.clientY })}
              onMouseLeave={() => setHover(null)}
            >
              <rect x={i * bw} y={0} width={bw} height={H} fill="transparent" />
              {segs}
              {(i % tickEvery === 0) && (
                <text x={i * bw + bw / 2} y={H + 16} textAnchor="middle" className="tick">{bucketLabel(bk, gran)}</text>
              )}
              {hover?.b === bk && <rect x={i * bw} y={0} width={bw} height={H} className="hoverband" />}
            </g>
          )
        })}
      </svg>
      {hover && (
        <div className="tip" style={{ left: Math.min(hover.x + 14, window.innerWidth - 300), top: hover.y + 14 }}>
          <div className="path">
            {gran === 'week' ? `wk of ${bucketLabel(hover.b, gran)}` : bucketLabel(hover.b, gran)}
          </div>
          <div className="nums">
            {[...(byBucket.get(hover.b) ?? new Map<string, number>())]
              .sort((a, b) => b[1] - a[1])
              .slice(0, 5)
              .map(([k, b]) => `${k} ${fmtBytes(b)}`)
              .join(' · ')}
          </div>
        </div>
      )}
    </div>
  )
}
