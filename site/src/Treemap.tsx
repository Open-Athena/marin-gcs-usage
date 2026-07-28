import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { dateColor, dateGradientCss, epochDaysToMonth, inkFor, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import { foldSmall, squarify } from './squarify'
import type { ColorMode, TreeNode } from './types'
import { TEAM_VARS, domTeam, fmtBytes, fmtN } from './types'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']
const WHITE_INK = ['--s1', '--s2', '--s6', '--s7', '--s8']
const TEAM_WHITE_INK = ['--t-core', '--t-stanford', '--t-oa']

interface Tip {
  x: number
  y: number
  path: string
  node: TreeNode
}

export interface DateRange { min: number; max: number }

export function Treemap({ root, mode, userIdx, dateRange }: {
  root: TreeNode
  mode: ColorMode
  userIdx: Map<string, UserIndexEntry>
  dateRange: DateRange | null
}) {
  const [path, setPath] = useState<TreeNode[]>([root])
  const [tip, setTip] = useState<Tip | null>(null)
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  const mapRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const node = path[path.length - 1]

  // Fixed category colors: global top-level dirs by total size.
  const catSlot = useMemo(() => {
    const catBytes = new Map<string, number>()
    for (const bucket of root.c ?? [])
      for (const d of bucket.c ?? []) {
        const k = d.n.startsWith('(') ? '(other)' : d.n
        catBytes.set(k, (catBytes.get(k) ?? 0) + d.b)
      }
    const cats = [...catBytes.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([k]) => k)
      .filter(k => k !== '(other)')
    return new Map(cats.slice(0, 8).map((k, i) => [k, SLOTS[i]]))
  }, [root])

  useEffect(() => {
    setPath([root])
    setTip(null)
  }, [root])

  useEffect(() => {
    const el = mapRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === 'Backspace' || e.key === 'Escape') && path.length > 1) {
        setPath(p => p.slice(0, -1))
        setTip(null)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [path.length])

  const slotOf = useCallback(
    (kidPath: TreeNode[]): string | null => {
      // kidPath: [root, bucket, d1, …]
      const top = kidPath[2]
      if (!top) return null
      const k = top.n.startsWith('(') ? '(other)' : top.n
      return catSlot.get(k) ?? null
    },
    [catSlot],
  )

  const rects = useMemo(
    () => squarify(foldSmall(node.c ?? [], size.w, size.h), 0, 0, size.w, size.h),
    [node, size],
  )

  // group roll-up for the current view: users in user modes, teams otherwise
  const rollup = useMemo(() => {
    if (!node.tm) return []
    if (mode === 'user' || mode === 'uteam') {
      const us = node.us ?? []
      const unattr = node.tm['unattributed'] ?? 0
      const attributed = Object.entries(node.tm)
        .filter(([t]) => t !== 'unattributed')
        .reduce((s, [, b]) => s + b, 0)
      const other = attributed - us.reduce((s, [, b]) => s + b, 0)
      return [
        ...us.map(([u, b]) => ({ k: u, b, col: userColor(u, userIdx, mode === 'uteam') })),
        ...(other > 0 ? [{ k: '(other attributed)', b: other, col: 'var(--other)' }] : []),
        ...(unattr > 0 ? [{ k: 'unattributed', b: unattr, col: 'var(--t-unattr)' }] : []),
      ].sort((a, b) => b.b - a.b)
    }
    return Object.entries(node.tm).map(([t, b]) => ({ k: t, b, col: `var(${TEAM_VARS[t] ?? '--t-unattr'})` }))
  }, [node, mode, userIdx])

  const cell = (kid: TreeNode, kidPath: TreeNode[], r: { x: number; y: number; w: number; h: number }, depth: number) => {
    const showLbl = r.w > 36 && r.h > 13
    // recurse while the cell has room — depth adapts to cell size
    const kw = r.w - 6
    const kh = r.h - (showLbl ? 23 : 6)
    const kids = kid.c && r.w > 90 && r.h > 44
      ? squarify(foldSmall(kid.c, kw, kh), 0, 0, kw, kh)
      : []
    let col: string
    let ink: string
    if (mode === 'tree') {
      const slot = slotOf(kidPath)
      col = slot ? `var(${slot})` : 'var(--other)'
      ink = slot ? (WHITE_INK.includes(slot) ? '#fff' : 'var(--cell-ink)') : 'var(--ink)'
    } else if (kids.length > 0) {
      // container: neutral so the nested tiles carry the data colors
      col = 'var(--panel)'
      ink = 'var(--ink)'
    } else if (mode === 'team') {
      const team = domTeam(kid)
      const tv = (team && TEAM_VARS[team]) || '--t-unattr'
      col = `var(${tv})`
      ink = TEAM_WHITE_INK.includes(tv) ? '#fff' : 'var(--ink)'
    } else if (mode === 'date') {
      if (kid.d != null && dateRange && dateRange.max > dateRange.min) {
        col = dateColor((kid.d - dateRange.min) / (dateRange.max - dateRange.min))
        ink = inkFor(col)
      } else {
        col = 'var(--other)'
        ink = 'var(--ink)'
      }
    } else {
      // user / uteam: a cell only takes a user's color when it is wholly
      // (~100%) theirs — mixed boxes stay gray until you drill/zoom
      const [u, ub] = kid.us?.[0] ?? [null, 0]
      col = userColor(ub >= 0.98 * kid.b ? u : null, userIdx, mode === 'uteam')
      ink = inkFor(col)
    }
    const gsPath = 'gs://' + kidPath.slice(1).map(n => n.n).join('/')
    const showTip = (e: React.MouseEvent) => {
      e.stopPropagation()
      setTip({ x: e.clientX, y: e.clientY, path: gsPath, node: kid })
    }
    const drill = kid.c
      ? (e: React.SyntheticEvent) => {
          e.stopPropagation()
          setTip(null)
          setPath(kidPath)
        }
      : undefined
    const dust = Math.min(r.w, r.h) < 14
    return (
      <div
        key={gsPath}
        className={'cell' + (kid.c ? ' branch' : '')}
        style={{
          left: r.x, top: r.y,
          width: Math.max(0, r.w - (dust ? 1 : 2)), height: Math.max(0, r.h - (dust ? 1 : 2)),
          background: col, color: ink, opacity: depth > 0 ? 0.82 : 0.92,
          ...(dust && { boxShadow: 'none', borderRadius: 1.5 }),
        }}
        tabIndex={0}
        onMouseMove={showTip}
        onMouseLeave={() => setTip(null)}
        onClick={drill}
        onKeyDown={e => e.key === 'Enter' && drill?.(e)}
      >
        {showLbl && (
          <div className={'lbl' + (r.w < 64 ? ' sm' : '')}>
            {kid.n}{r.w > 90 && <span className="sz"> {fmtBytes(kid.b)}</span>}
          </div>
        )}
        {kids.length > 0 && (
          <div className="inner" style={{ top: showLbl ? 20 : 3 }}>
            {kids.filter(s => s.w >= 3 && s.h >= 3).map(s =>
              cell(s.it, [...kidPath, s.it], s, depth + 1),
            )}
          </div>
        )}
      </div>
    )
  }

  const fullscreen = () => {
    const el = wrapRef.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else void el.requestFullscreen()
  }

  return (
    <div className="treemap" ref={wrapRef}>
      <div className="bar">
        <nav className="crumbs" aria-label="Path">
          {path.map((n, i) => (
            <span key={i}>
              {i > 0 && <span className="sep">/</span>}
              {i < path.length - 1 ? (
                <a tabIndex={0} onClick={() => setPath(path.slice(0, i + 1))}>{n.n}</a>
              ) : (
                <span className="cur">{n.n}</span>
              )}
            </span>
          ))}
          <span className="sep"> — {fmtBytes(node.b)} · {fmtN(node.o)} objects</span>
        </nav>
        <div className="legend">
          {mode === 'team' ? (
            Object.entries(TEAM_VARS).map(([t, v]) => (
              <span className="li" key={t}>
                <span className="sw" style={{ background: `var(${v})` }} />
                {t}
              </span>
            ))
          ) : mode === 'date' && dateRange ? (
            <span className="li gradli">
              {epochDaysToMonth(dateRange.min)}
              <span className="gradbar" style={{ background: dateGradientCss() }} />
              {epochDaysToMonth(dateRange.max)}
            </span>
          ) : mode === 'user' || mode === 'uteam' ? (
            <>
              {[...userIdx.entries()].slice(0, 10).map(([u]) => (
                <span className="li" key={u}>
                  <span className="sw" style={{ background: userColor(u, userIdx, mode === 'uteam') }} />
                  {u}
                </span>
              ))}
              <span className="li"><span className="sw" style={{ background: 'var(--t-unattr)' }} />unattributed</span>
            </>
          ) : (
            <>
              {[...catSlot.entries()].map(([k, s]) => (
                <span className="li" key={k}>
                  <span className="sw" style={{ background: `var(${s})` }} />
                  {k}
                </span>
              ))}
              <span className="li"><span className="sw" style={{ background: 'var(--other)' }} />other</span>
            </>
          )}
        </div>
        <button className="fs" onClick={fullscreen} title="Toggle fullscreen">⛶</button>
      </div>
      {rollup.length > 0 && (
        <div className="rollup">
          {rollup.filter(r => r.b >= 0.001 * node.b).map(r => (
            <span className="ri" key={r.k}>
              <span className="sw" style={{ background: r.col }} />
              {r.k} <b>{fmtBytes(r.b)}</b>
              <span className="pct">{((100 * r.b) / node.b).toFixed(1)}%</span>
            </span>
          ))}
        </div>
      )}
      <div className="map" ref={mapRef} role="application" aria-label="Storage treemap">
        {rects
          .filter(r => r.w >= 3 && r.h >= 3)
          .map(r => cell(r.it, [...path, r.it], r, 0))}
      </div>
      {tip && (() => {
        const userMode = mode === 'user' || mode === 'uteam'
        const teams = tip.node.tm && (
          <div className="teams">
            {Object.entries(tip.node.tm)
              .filter(([, b]) => b >= 0.005 * tip.node.b)
              .map(([t, b]) => (
                <span className="tt-team" key={t}>
                  <span className="sw" style={{ background: `var(${TEAM_VARS[t] ?? '--t-unattr'})` }} />
                  {t} {((100 * b) / tip.node.b).toFixed(0)}%
                </span>
              ))}
          </div>
        )
        const users = tip.node.us && tip.node.us.length > 0 && (
          <div className="users">
            {tip.node.us.map(([u, b]) => (
              <div className="tt-user" key={u}>
                {userMode && <span className="sw" style={{ background: userColor(u, userIdx, mode === 'uteam') }} />}
                {u} · {fmtBytes(b)}
              </div>
            ))}
          </div>
        )
        return (
          <div
            className="tip"
            style={{
              left: Math.min(tip.x + 14, window.innerWidth - 320),
              top: Math.min(tip.y + 14, window.innerHeight - 80),
            }}
          >
            <div className="path">
              <span className="dirname">{tip.path.slice(0, tip.path.lastIndexOf('/') + 1)}</span>
              <span className="basename">{tip.path.slice(tip.path.lastIndexOf('/') + 1)}</span>
            </div>
            <div className="nums">
              {fmtBytes(tip.node.b)} · {fmtN(tip.node.o)} objects · {((100 * tip.node.b) / root.b).toFixed(2)}% of total
              {tip.node.d != null && <> · mean created {epochDaysToMonth(tip.node.d)}</>}
            </div>
            {userMode ? <>{users}{teams}</> : <>{teams}{users}</>}
          </div>
        )
      })()}
      <div className="hint">click to drill in · click the path (or Backspace) to go up · cells &lt;20 GB folded into “(other)”</div>
    </div>
  )
}
