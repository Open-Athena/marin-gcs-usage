import { useMemo, useState } from 'react'
import type { AgeRow, ColorMode } from './types'
import { TEAM_VARS, fmtBytes } from './types'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']

/** Stacked monthly bars: bytes by created month, split by top-level dir or owning team. */
export function AgeChart({ rows, catOrder, mode }: { rows: AgeRow[]; catOrder: string[]; mode: ColorMode }) {
  const [hover, setHover] = useState<{ m: string; x: number; y: number } | null>(null)

  const { months, byMonth, slotOf, legend } = useMemo(() => {
    const slotMap = new Map(catOrder.slice(0, 8).map((k, i) => [k, SLOTS[i]]))
    const teamMode = mode === 'team'
    const keyOf = (r: AgeRow) =>
      teamMode ? (r.t ?? 'unattributed') : slotMap.has(r.d1) ? r.d1 : '(other)'
    const byMonth = new Map<string, Map<string, number>>()
    for (const r of rows) {
      const k = keyOf(r)
      const m = byMonth.get(r.m) ?? new Map<string, number>()
      m.set(k, (m.get(k) ?? 0) + r.b)
      byMonth.set(r.m, m)
    }
    const months = [...byMonth.keys()].sort()
    const slotOf = (k: string) => (teamMode ? (TEAM_VARS[k] ?? '--t-unattr') : slotMap.get(k))
    const legend: [string, string][] = teamMode
      ? Object.entries(TEAM_VARS)
      : [...catOrder.slice(0, 8).map((k, i): [string, string] => [k, SLOTS[i]]), ['(other)', '--other']]
    return { months, byMonth, slotOf, legend }
  }, [rows, catOrder, mode])

  const maxB = useMemo(
    () => Math.max(...months.map(m => [...byMonth.get(m)!.values()].reduce((a, b) => a + b, 0))),
    [months, byMonth],
  )

  const W = 900
  const H = 220
  const bw = W / Math.max(months.length, 1)

  return (
    <div className="agechart">
      <div className="legend">
        {legend.map(([k, v]) => (
          <span className="li" key={k}>
            <span className="sw" style={{ background: `var(${v})` }} />
            {k}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H + 24}`} preserveAspectRatio="none" role="img" aria-label="Bytes by created month">
        {months.map((m, i) => {
          const parts = byMonth.get(m)!
          const total = [...parts.values()].reduce((a, b) => a + b, 0)
          let y = H
          const segs = [...parts.entries()].sort((a, b) => b[1] - a[1]).map(([k, b]) => {
            const h = (b / maxB) * H
            y -= h
            return <rect key={k} x={i * bw + 1} y={y} width={Math.max(bw - 2, 1)} height={Math.max(h - 1, 0)} fill={`var(${slotOf(k) ?? '--other'})`} rx={1.5} />
          })
          return (
            <g
              key={m}
              onMouseMove={e => setHover({ m, x: e.clientX, y: e.clientY })}
              onMouseLeave={() => setHover(null)}
            >
              <rect x={i * bw} y={0} width={bw} height={H} fill="transparent" />
              {segs}
              {(i % Math.ceil(months.length / 12) === 0) && (
                <text x={i * bw + bw / 2} y={H + 16} textAnchor="middle" className="tick">{m}</text>
              )}
              {hover?.m === m && <rect x={i * bw} y={0} width={bw} height={H} className="hoverband" />}
            </g>
          )
        })}
      </svg>
      {hover && (
        <div className="tip" style={{ left: Math.min(hover.x + 14, window.innerWidth - 300), top: hover.y + 14 }}>
          <div className="path">{hover.m}</div>
          <div className="nums">
            {[...(byMonth.get(hover.m) ?? new Map())]
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
