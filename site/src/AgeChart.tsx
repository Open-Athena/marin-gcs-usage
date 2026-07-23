import { useMemo, useState } from 'react'
import { dateColor, dateGradientCss, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import type { AgeRow, ColorMode } from './types'
import { TEAM_VARS, fmtBytes } from './types'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']

const dateBarColor = (i: number, n: number): string => dateColor(n > 1 ? i / (n - 1) : 1)

/** Stacked monthly bars: bytes by created month, split per the active color mode. */
export function AgeChart({ rows, catOrder, mode, userIdx }: {
  rows: AgeRow[]
  catOrder: string[]
  mode: ColorMode
  userIdx: Map<string, UserIndexEntry>
}) {
  const [hover, setHover] = useState<{ m: string; x: number; y: number } | null>(null)

  const { months, byMonth, colorOf, legend } = useMemo(() => {
    const slotMap = new Map(catOrder.slice(0, 8).map((k, i) => [k, SLOTS[i]]))
    const userMode = mode === 'user' || mode === 'uteam'
    const keyOf = (r: AgeRow) =>
      mode === 'team' ? (r.t ?? 'unattributed')
      : userMode ? (r.u ?? 'unattributed')
      : slotMap.has(r.d1) ? r.d1 : '(other)'
    const byMonth = new Map<string, Map<string, number>>()
    for (const r of rows) {
      const k = keyOf(r)
      const m = byMonth.get(r.m) ?? new Map<string, number>()
      m.set(k, (m.get(k) ?? 0) + r.b)
      byMonth.set(r.m, m)
    }
    const months = [...byMonth.keys()].sort()
    const colorOf = (k: string): string =>
      mode === 'team' ? `var(${TEAM_VARS[k] ?? '--t-unattr'})`
      : userMode ? (k === 'unattributed' ? 'var(--t-unattr)' : userColor(k, userIdx, mode === 'uteam'))
      : `var(${slotMap.get(k) ?? '--other'})`
    const legend: [string, string][] =
      mode === 'team' ? Object.entries(TEAM_VARS).map(([t, v]): [string, string] => [t, `var(${v})`])
      : userMode
        ? [
            ...[...userIdx.keys()].slice(0, 10).map((u): [string, string] => [u, userColor(u, userIdx, mode === 'uteam')]),
            ['unattributed', 'var(--t-unattr)'],
          ]
        : [
            ...catOrder.slice(0, 8).map((k, i): [string, string] => [k, `var(${SLOTS[i]})`]),
            ['(other)', 'var(--other)'],
          ]
    return { months, byMonth, colorOf, legend }
  }, [rows, catOrder, mode, userIdx])

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
      </div>
      <svg viewBox={`0 0 ${W} ${H + 24}`} preserveAspectRatio="none" role="img" aria-label="Bytes by created month">
        {months.map((m, i) => {
          const parts = byMonth.get(m)!
          const total = [...parts.values()].reduce((a, b) => a + b, 0)
          let y = H
          const segs = [...parts.entries()].sort((a, b) => b[1] - a[1]).map(([k, b]) => {
            const h = (b / maxB) * H
            y -= h
            return <rect key={k} x={i * bw + 1} y={y} width={Math.max(bw - 2, 1)} height={Math.max(h - 1, 0)} fill={mode === 'date' ? dateBarColor(i, months.length) : colorOf(k)} rx={1.5} />
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
