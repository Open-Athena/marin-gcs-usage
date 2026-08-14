import { useCallback, useMemo } from 'react'
import { Treemap as DtTreemap } from '@disk-tree/react'
import type { CellCtx, CellStyle } from '@disk-tree/react'
import { dateColor, dateGradientCss, epochDaysToMonth, inkFor, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import { ClassMixTip, Tooltip } from './Tooltip'
import type { ColorMode, Pricing, TreeNode } from './types'
import { CLASS_NAMES, TEAM_VARS, classMix, domTeamSeg, fmtBytes, fmtN, fmtUsd, groupLabel, ratePerByte, sharedColor } from './types'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']
const WHITE_INK = ['--s1', '--s2', '--s6', '--s7', '--s8']
const TEAM_WHITE_INK = ['--t-stanford', '--t-oa', '--t-communal']

export interface DateRange { min: number; max: number }

export interface Highlight {
  user?: string
  team?: string
}

// Domain wrapper over @disk-tree/react's generic <Treemap>: all layout,
// drill/crumb state, hover-pinning, folding, and keyboard nav live upstream;
// this file supplies marin's business logic (attribution color modes, class
// lens, $-pricing, rollup bar, tooltip content) through the accessor props.
export function Treemap({ root, mode, userIdx, dateRange, hl, pricing, lens, redact }: {
  root: TreeNode
  mode: ColorMode
  userIdx: Map<string, UserIndexEntry>
  dateRange: DateRange | null
  hl?: Highlight | null
  pricing?: Pricing | null
  lens?: boolean
  // OG-image mode: hide every text detail (cell labels, crumb/rollup bars, hint)
  // and render just the colored cells. Never set by the live app.
  redact?: boolean
}) {
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

  const gsPathOf = (path: TreeNode[]) => 'gs://' + path.slice(1).map(n => n.n).join('/')

  // Fold merger: first-class TreeNode aggregating tm/us/d so folded tiles keep
  // real tooltips (upstream calls this at every nesting level).
  const mergeSmall = useCallback((tiny: TreeNode[]): TreeNode => {
    const b = tiny.reduce((s, it) => s + it.b, 0)
    const o = tiny.reduce((s, it) => s + it.o, 0)
    const tm: Record<string, number> = {}
    const us: Record<string, number> = {}
    let wd = 0
    let wdb = 0
    for (const it of tiny) {
      for (const [t, tb] of Object.entries(it.tm ?? {})) tm[t] = (tm[t] ?? 0) + tb
      for (const [u, ub] of it.us ?? []) us[u] = (us[u] ?? 0) + ub
      if (it.d != null) {
        wd += it.d * it.b
        wdb += it.b
      }
    }
    const folded: TreeNode = { n: `(+${tiny.length})`, b, o }
    if (wdb) folded.d = Math.round(wd / wdb)
    if (Object.keys(tm).length) folded.tm = tm
    const topUs = Object.entries(us).sort((a, c) => c[1] - a[1]).slice(0, 5)
    if (topUs.length) folded.us = topUs as [string, number][]
    return folded
  }, [])

  const colorForCell = useCallback(
    (kid: TreeNode, kidPath: TreeNode[], _depth: number, ctx: CellCtx): CellStyle => {
      let bg: string
      let ink: string
      if (mode === 'tree') {
        const slot = slotOf(kidPath)
        bg = slot ? `var(${slot})` : 'var(--other)'
        ink = slot ? (WHITE_INK.includes(slot) ? '#fff' : 'var(--cell-ink)') : 'var(--ink)'
      } else if (ctx.hasKids) {
        // container: neutral so the nested tiles carry the data colors
        bg = 'var(--panel)'
        ink = 'var(--ink)'
      } else if (mode === 'team') {
        const seg = domTeamSeg(kid)
        const tv = (seg && TEAM_VARS[seg.team]) || '--t-unattr'
        bg = seg?.shared ? sharedColor(tv) : `var(${tv})`
        ink = !seg?.shared && TEAM_WHITE_INK.includes(tv) ? '#fff' : 'var(--ink)'
      } else if (mode === 'date') {
        if (kid.d != null && dateRange && dateRange.max > dateRange.min) {
          bg = dateColor((kid.d - dateRange.min) / (dateRange.max - dateRange.min))
          ink = inkFor(bg)
        } else {
          bg = 'var(--other)'
          ink = 'var(--ink)'
        }
      } else {
        // user / uteam: a cell only takes a user's color when it is wholly
        // (~100%) theirs — mixed boxes stay gray until you drill/zoom
        const [u, ub] = kid.us?.[0] ?? [null, 0]
        bg = userColor(ub >= 0.98 * kid.b ? u : null, userIdx, mode === 'uteam')
        ink = inkFor(bg)
      }
      // class lens: hatch by colder-class (non-STANDARD) byte fraction — leaf
      // cells only (cells are semi-transparent, so a parent hatch would bleed
      // through all-STANDARD children)
      const coldFrac = lens && !ctx.hasKids ? Object.values(kid.cb ?? {}).reduce((a, b) => a + b, 0) / kid.b : 0
      const hatch = coldFrac > 0.01
        ? `repeating-linear-gradient(135deg, rgb(120 170 255 / ${(0.18 + 0.5 * coldFrac).toFixed(2)}) 0 4px, transparent 4px 9px)`
        : undefined
      // highlight mode: leaf cells not majority-owned by the selected user/team fade back
      let dim = false
      if (hl && !ctx.hasKids) {
        if (hl.user) dim = (kid.us?.find(([u]) => u === hl.user)?.[1] ?? 0) < 0.5 * kid.b
        else if (hl.team) dim = (kid.tm?.[hl.team] ?? 0) < 0.5 * kid.b
      }
      return { bg, ink, hatch, opacity: dim ? 0.22 : undefined }
    },
    [mode, slotOf, userIdx, dateRange, hl, lens],
  )

  // group roll-up for the current view: users in user modes, teams otherwise;
  // $ figures use class-aware per-group rates when the snapshot carries them
  const rollupFor = (node: TreeNode) => {
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
        const gl = groupLabel(t)
        return s > 0 && b - s > 0
          ? [
              { k: `${gl} (users)`, b: b - s, col: `var(${tv})`, rate, mix },
              { k: `${gl} (shared)`, b: s, col: sharedColor(tv), rate, mix },
            ]
          : s > 0
            ? [{ k: gl, b: s, col: sharedColor(tv), rate, mix }]  // all-shared group (communal): no redundant "(shared)"
            : [{ k: gl, b, col: `var(${tv})`, rate, mix }]
      })
      .sort((a, b) => b.b - a.b)
  }

  const renderRollup = (node: TreeNode) => {
    if (redact) return null
    const rollup = rollupFor(node)
    if (!rollup.length) return null
    return (
      <>
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
      </>
    )
  }

  /* Legend only for modes where it isn't a strict subset of the roll-up bar:
     team + user modes are dropped (the roll-up already shows the same
     swatch+label, plus size and $). tree (prefix colors) and age (date
     gradient) convey distinct keys, so they keep the legend. */
  const legend = mode !== 'team' && mode !== 'user' && mode !== 'uteam'
    ? () => (
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
      )
    : undefined

  const renderTooltip = (n: TreeNode, path: TreeNode[]) => {
    const gsPath = gsPathOf(path)
    const userMode = mode === 'user' || mode === 'uteam'
    const mix = classMix(n)
    const classes = n.cb && (
      <div className="classes-row">
        {Object.entries(mix).map(([c, b]) => (
          <span className="tt-cls" key={c}>
            {CLASS_NAMES[c]} {((100 * b) / n.b).toFixed(0)}%
          </span>
        ))}
        {pricing && <span className="usd">{fmtUsd(n.b * ratePerByte(mix))}/mo</span>}
      </div>
    )
    const teams = n.tm && (
      <div className="teams">
        {Object.entries(n.tm)
          .filter(([, b]) => b >= 0.005 * n.b)
          .map(([t, b]) => {
            const s = n.sh?.[t] ?? 0
            return (
              <span className="tt-team" key={t}>
                <span className="sw" style={{ background: `var(${TEAM_VARS[t] ?? '--t-unattr'})` }} />
                {t} {((100 * b) / n.b).toFixed(0)}%
                {s >= 0.01 * b && <span className="shr"> ({((100 * s) / b).toFixed(0)}% shared)</span>}
              </span>
            )
          })}
      </div>
    )
    const users = n.us && n.us.length > 0 && (
      <div className="users">
        {n.us.map(([u, b]) => (
          <div className="tt-user" key={u}>
            {userMode && <span className="sw" style={{ background: userColor(u, userIdx, mode === 'uteam') }} />}
            {u} · {fmtBytes(b)}
          </div>
        ))}
      </div>
    )
    return (
      <>
        <div className="path">
          <span className="dirname">{gsPath.slice(0, gsPath.lastIndexOf('/') + 1)}</span>
          <span className="basename">{gsPath.slice(gsPath.lastIndexOf('/') + 1)}</span>
        </div>
        <div className="nums">
          {fmtBytes(n.b)} · {fmtN(n.o)} objects · {((100 * n.b) / root.b).toFixed(2)}% of total
          {n.d != null && <> · mean created {epochDaysToMonth(n.d)}</>}
        </div>
        {classes}
        {userMode ? <>{users}{teams}</> : <>{teams}{users}</>}
      </>
    )
  }

  return (
    <DtTreemap<TreeNode>
      root={root}
      getSize={n => n.b}
      getChildren={n => n.c}
      getLabel={n => n.n}
      getId={(_n, p) => gsPathOf(p)}
      formatSize={fmtBytes}
      mergeSmall={mergeSmall}
      colorForCell={colorForCell}
      renderTooltip={renderTooltip}
      renderRollup={renderRollup}
      renderLegend={redact ? undefined : legend}
      renderCrumbSuffix={node => (
        <>
          — {fmtBytes(node.b)} · {fmtN(node.o)} objects
          {pricing && <> · est. {fmtUsd(node.b * pricing.blended)}/mo</>}
        </>
      )}
      renderFooter={redact
        ? undefined
        : () => <div className="hint">click to drill in · click a leaf to pin its details · click the path (or Backspace) to go up · cells &lt;20 GB folded into “(other)”</div>}
      chrome={!redact}
      showLabels={!redact}
      className="treemap"
    />
  )
}
