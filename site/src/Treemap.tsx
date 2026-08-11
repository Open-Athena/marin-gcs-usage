import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { dateColor, dateGradientCss, epochDaysToMonth, inkFor, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import { useHoverPin } from './hoverpin/useHoverPin'
import { ClassMixTip, Tooltip } from './Tooltip'
import { foldSmall, squarify } from './squarify'
import type { ColorMode, Pricing, TreeNode } from './types'
import { CLASS_NAMES, TEAM_VARS, classMix, domTeamSeg, fmtBytes, fmtN, fmtUsd, ratePerByte, sharedColor } from './types'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']
const WHITE_INK = ['--s1', '--s2', '--s6', '--s7', '--s8']
const TEAM_WHITE_INK = ['--t-core', '--t-stanford', '--t-oa', '--t-communal']

interface Tip {
  x: number
  y: number
  path: string
  node: TreeNode
}

export interface DateRange { min: number; max: number }

export interface Highlight {
  user?: string
  team?: string
}

export function Treemap({ root, mode, userIdx, dateRange, hl, pricing, lens }: {
  root: TreeNode
  mode: ColorMode
  userIdx: Map<string, UserIndexEntry>
  dateRange: DateRange | null
  hl?: Highlight | null
  pricing?: Pricing | null
  lens?: boolean
}) {
  const [path, setPath] = useState<TreeNode[]>([root])
  const [tip, setTip] = useState<Tip | null>(null)
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  const mapRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  // pin state keyed by gs:// path; the pinned Tip snapshot freezes position/content
  const pin = useHoverPin<string>({ excludeRefs: [tipRef] })
  const [pinnedTip, setPinnedTip] = useState<Tip | null>(null)
  useEffect(() => {
    if (pin.pinned === null) setPinnedTip(null)
  }, [pin.pinned])
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

  // group roll-up for the current view: users in user modes, teams otherwise;
  // $ figures use class-aware per-group rates when the snapshot carries them
  const rollup = useMemo(() => {
    if (!node.tm) return []
    const teamRate = (t: string) => pricing && (pricing.teamRates?.[t] ?? pricing.blended)
    const userRate = (u: string) => pricing && (pricing.userRates?.[u] ?? pricing.blended)
    if (mode === 'user' || mode === 'uteam') {
      const us = node.us ?? []
      const unattr = node.tm['unattributed'] ?? 0
      const attributed = Object.entries(node.tm)
        .filter(([t]) => t !== 'unattributed')
        .reduce((s, [, b]) => s + b, 0)
      const other = attributed - us.reduce((s, [, b]) => s + b, 0)
      return [
        ...us.map(([u, b]) => ({ k: u, b, col: userColor(u, userIdx, mode === 'uteam'), rate: userRate(u), mix: pricing?.userMix?.[u] })),
        ...(other > 0 ? [{ k: '(other attributed)', b: other, col: 'var(--other)', rate: pricing?.blended, mix: undefined }] : []),
        ...(unattr > 0 ? [{ k: 'unattributed', b: unattr, col: 'var(--t-unattr)', rate: teamRate('unattributed'), mix: pricing?.teamMix?.['unattributed'] }] : []),
      ].sort((a, b) => b.b - a.b)
    }
    return Object.entries(node.tm)
      .flatMap(([t, b]) => {
        const tv = TEAM_VARS[t] ?? '--t-unattr'
        const s = node.sh?.[t] ?? 0
        const rate = teamRate(t)
        const mix = pricing?.teamMix?.[t]
        return s > 0 && b - s > 0
          ? [
              { k: `${t} (users)`, b: b - s, col: `var(${tv})`, rate, mix },
              { k: `${t} (shared)`, b: s, col: sharedColor(tv), rate, mix },
            ]
          : s > 0
            ? [{ k: t, b: s, col: sharedColor(tv), rate, mix }]  // all-shared group (communal): no redundant "(shared)"
            : [{ k: t, b, col: `var(${tv})`, rate, mix }]
      })
      .sort((a, b) => b.b - a.b)
  }, [node, mode, userIdx, pricing])

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
      const seg = domTeamSeg(kid)
      const tv = (seg && TEAM_VARS[seg.team]) || '--t-unattr'
      col = seg?.shared ? sharedColor(tv) : `var(${tv})`
      ink = !seg?.shared && TEAM_WHITE_INK.includes(tv) ? '#fff' : 'var(--ink)'
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
      pin.hover(gsPath)
      setTip({ x: e.clientX, y: e.clientY, path: gsPath, node: kid })
    }
    // branch cells drill on click (existing UX); leaf cells pin their tooltip
    const drill = kid.c
      ? (e: React.SyntheticEvent) => {
          e.stopPropagation()
          setTip(null)
          pin.clearPin()
          setPath(kidPath)
        }
      : (e: React.SyntheticEvent) => {
          e.stopPropagation()
          const me = e as React.MouseEvent
          pin.togglePin(gsPath)
          setPinnedTip(p => (p?.path === gsPath ? null : { x: me.clientX, y: me.clientY, path: gsPath, node: kid }))
        }
    const dust = Math.min(r.w, r.h) < 14
    // class lens: hatch by colder-class (non-STANDARD) byte fraction — leaf
    // cells only (cells are semi-transparent, so a parent hatch would bleed
    // through all-STANDARD children)
    const coldFrac = lens && kids.length === 0 ? Object.values(kid.cb ?? {}).reduce((a, b) => a + b, 0) / kid.b : 0
    const hatch = coldFrac > 0.01
      ? `repeating-linear-gradient(135deg, rgb(120 170 255 / ${(0.18 + 0.5 * coldFrac).toFixed(2)}) 0 4px, transparent 4px 9px)`
      : undefined
    // highlight mode: leaf cells not majority-owned by the selected user/team fade back
    let dim = false
    if (hl && kids.length === 0) {
      if (hl.user) dim = (kid.us?.find(([u]) => u === hl.user)?.[1] ?? 0) < 0.5 * kid.b
      else if (hl.team) dim = (kid.tm?.[hl.team] ?? 0) < 0.5 * kid.b
    }
    return (
      <div
        key={gsPath}
        className={'cell' + (kid.c ? ' branch' : '')}
        style={{
          left: r.x, top: r.y,
          width: Math.max(0, r.w - (dust ? 1 : 2)), height: Math.max(0, r.h - (dust ? 1 : 2)),
          background: col, color: ink, opacity: (depth > 0 ? 0.82 : 0.92) * (dim ? 0.22 : 1),
          ...(hatch && { backgroundImage: hatch }),
          ...(dust && { boxShadow: 'none', borderRadius: 1.5 }),
        }}
        tabIndex={0}
        onMouseMove={showTip}
        onMouseLeave={() => {
          pin.hover(null)
          setTip(null)
        }}
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
          <span className="sep">
            {' '}— {fmtBytes(node.b)} · {fmtN(node.o)} objects
            {pricing && <> · est. {fmtUsd(node.b * pricing.blended)}/mo</>}
          </span>
        </nav>
        {/* Legend only for modes where it isn't a strict subset of the roll-up
            bar below: team + user modes are dropped (the roll-up already shows
            the same swatch+label, plus size and $). tree (prefix colors) and
            age (date gradient) convey distinct keys, so they keep the legend. */}
        {mode !== 'team' && mode !== 'user' && mode !== 'uteam' && (
          <div className="legend">
            {mode === 'date' && dateRange ? (
              <span className="li gradli">
                {epochDaysToMonth(dateRange.min)}
                <span className="gradbar" style={{ background: dateGradientCss() }} />
                {epochDaysToMonth(dateRange.max)}
              </span>
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
        )}
        <button className="fs" onClick={fullscreen} title="Toggle fullscreen">⛶</button>
      </div>
      {rollup.length > 0 && (
        <div className="rollup">
          {rollup.filter(r => r.b >= 0.001 * node.b).map(r => (
            <span className="ri" key={r.k}>
              <span className="sw" style={{ background: r.col }} />
              {r.k} <b>{fmtBytes(r.b)}</b>
              <span className="pct">{((100 * r.b) / node.b).toFixed(1)}%</span>
              {r.rate != null && (
                r.mix ? (
                  <Tooltip content={<ClassMixTip mix={r.mix} note="the group's fleet-wide class mix sets its $/byte rate; $ shown = rate × this view's bytes" />}>
                    <span className="usd dotted">{fmtUsd(r.b * r.rate)}/mo</span>
                  </Tooltip>
                ) : (
                  <span className="usd">{fmtUsd(r.b * r.rate)}/mo</span>
                )
              )}
            </span>
          ))}
        </div>
      )}
      <div className="map" ref={mapRef} role="application" aria-label="Storage treemap">
        {rects
          .filter(r => r.w >= 3 && r.h >= 3)
          .map(r => cell(r.it, [...path, r.it], r, 0))}
      </div>
      {(pinnedTip ?? tip) && (() => {
        const isPinned = pinnedTip !== null
        const tp = (pinnedTip ?? tip)!
        const userMode = mode === 'user' || mode === 'uteam'
        const mix = classMix(tp.node)
        const classes = tp.node.cb && (
          <div className="classes-row">
            {Object.entries(mix).map(([c, b]) => (
              <span className="tt-cls" key={c}>
                {CLASS_NAMES[c]} {((100 * b) / tp.node.b).toFixed(0)}%
              </span>
            ))}
            {pricing && <span className="usd">{fmtUsd(tp.node.b * ratePerByte(mix))}/mo</span>}
          </div>
        )
        const teams = tp.node.tm && (
          <div className="teams">
            {Object.entries(tp.node.tm)
              .filter(([, b]) => b >= 0.005 * tp.node.b)
              .map(([t, b]) => {
                const s = tp.node.sh?.[t] ?? 0
                return (
                  <span className="tt-team" key={t}>
                    <span className="sw" style={{ background: `var(${TEAM_VARS[t] ?? '--t-unattr'})` }} />
                    {t} {((100 * b) / tp.node.b).toFixed(0)}%
                    {s >= 0.01 * b && <span className="shr"> ({((100 * s) / b).toFixed(0)}% shared)</span>}
                  </span>
                )
              })}
          </div>
        )
        const users = tp.node.us && tp.node.us.length > 0 && (
          <div className="users">
            {tp.node.us.map(([u, b]) => (
              <div className="tt-user" key={u}>
                {userMode && <span className="sw" style={{ background: userColor(u, userIdx, mode === 'uteam') }} />}
                {u} · {fmtBytes(b)}
              </div>
            ))}
          </div>
        )
        return (
          <div
            ref={tipRef}
            className={'tip' + (isPinned ? ' pinned' : '')}
            style={{
              left: Math.min(tp.x + 14, window.innerWidth - 320),
              top: Math.min(tp.y + 14, window.innerHeight - 80),
            }}
          >
            {isPinned && (
              <button className="unpin" onClick={() => pin.clearPin()} title="Unpin (Esc)">×</button>
            )}
            <div className="path">
              <span className="dirname">{tp.path.slice(0, tp.path.lastIndexOf('/') + 1)}</span>
              <span className="basename">{tp.path.slice(tp.path.lastIndexOf('/') + 1)}</span>
            </div>
            <div className="nums">
              {fmtBytes(tp.node.b)} · {fmtN(tp.node.o)} objects · {((100 * tp.node.b) / root.b).toFixed(2)}% of total
              {tp.node.d != null && <> · mean created {epochDaysToMonth(tp.node.d)}</>}
            </div>
            {classes}
            {userMode ? <>{users}{teams}</> : <>{teams}{users}</>}
          </div>
        )
      })()}
      <div className="hint">click to drill in · click a leaf to pin its details · click the path (or Backspace) to go up · cells &lt;20 GB folded into “(other)”</div>
    </div>
  )
}
