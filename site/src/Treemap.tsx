import { useCallback, useMemo } from 'react'
import { Treemap as DtTreemap } from '@disk-tree/react'
import type { CellCtx, CellStyle } from '@disk-tree/react'
import { dateColor, dateGradientCss, epochDaysToMonth, inkFor, userColor } from './colors'
import type { UserIndex } from './colors'
import { ClassMixTip, Tooltip } from './Tooltip'
import type { ColorMode, Pricing, TreeNode } from './types'
import { CLASS_NAMES, classMix, fmtN, fmtUsd, ratePerByte } from './types'
import { UserChip } from './UserChip'
import { useTiling } from './tiling'
import { useUnits } from './units'

const SLOTS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8']
const WHITE_INK = ['--s1', '--s2', '--s6', '--s7', '--s8']

export interface DateRange { min: number; max: number }

export interface Highlight {
  user?: string
}

// Domain wrapper over @disk-tree/react's generic <Treemap>: all layout,
// drill/crumb state, hover-pinning, folding, and keyboard nav live upstream;
// this file supplies marin's business logic (attribution color modes, class
// lens, $-pricing, rollup bar, tooltip content) through the accessor props.
export function Treemap({ root, mode, userIdx, dateRange, hl, pricing, lens, redact, initialPath }: {
  // Start drilled here (the lone bucket) — crumbs keep the ancestry.
  initialPath?: TreeNode[]
  root: TreeNode
  mode: ColorMode
  userIdx: UserIndex
  dateRange: DateRange | null
  hl?: Highlight | null
  pricing?: Pricing | null
  lens?: boolean
  // OG-image mode: hide every text detail (cell labels, crumb/rollup bars, hint)
  // and render just the colored cells. Never set by the live app.
  redact?: boolean
}) {
  const { fmtBytes } = useUnits()
  // Tiling is a user preference (header toggle): `shared` by default.
  const [tiling] = useTiling()
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

  // this branch serves the CoreWeave (CAIOS) estate — object URLs are s3://
  const pathOf = (path: TreeNode[]) => 's3://' + path.slice(1).map(n => n.n).join('/')

  // Fold merger: first-class TreeNode aggregating us/d so folded tiles keep
  // real tooltips (upstream calls this at every nesting level).
  const mergeSmall = useCallback((tiny: TreeNode[]): TreeNode => {
    const b = tiny.reduce((s, it) => s + it.b, 0)
    const o = tiny.reduce((s, it) => s + it.o, 0)
    const us: Record<string, number> = {}
    let wd = 0
    let wdb = 0
    for (const it of tiny) {
      for (const [u, ub] of it.us ?? []) us[u] = (us[u] ?? 0) + ub
      if (it.d != null) {
        wd += it.d * it.b
        wdb += it.b
      }
    }
    const folded: TreeNode = { n: `(+${tiny.length})`, b, o }
    if (wdb) folded.d = Math.round(wd / wdb)
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
      } else if (mode === 'date') {
        if (kid.d != null && dateRange && dateRange.max > dateRange.min) {
          bg = dateColor((kid.d - dateRange.min) / (dateRange.max - dateRange.min))
          ink = inkFor(bg)
        } else {
          bg = 'var(--other)'
          ink = 'var(--ink)'
        }
      } else {
        // user: a cell only takes a user's color when it is wholly (~100%)
        // theirs — mixed boxes stay gray until you drill/zoom
        const [u, ub] = kid.us?.[0] ?? [null, 0]
        bg = userColor(ub >= 0.98 * kid.b ? u : null, userIdx)
        ink = inkFor(bg)
      }
      // class lens: hatch by colder-class (non-STANDARD) byte fraction — leaf
      // cells only (cells are semi-transparent, so a parent hatch would bleed
      // through all-STANDARD children)
      const coldFrac = lens && !ctx.hasKids ? Object.values(kid.cb ?? {}).reduce((a, b) => a + b, 0) / kid.b : 0
      const hatch = coldFrac > 0.01
        ? `repeating-linear-gradient(135deg, rgb(120 170 255 / ${(0.18 + 0.5 * coldFrac).toFixed(2)}) 0 4px, transparent 4px 9px)`
        : undefined
      // highlight mode: leaf cells not majority-owned by the selected user fade back
      let dim = false
      if (hl?.user && !ctx.hasKids) {
        dim = (kid.us?.find(([u]) => u === hl.user)?.[1] ?? 0) < 0.5 * kid.b
      }
      return { bg, ink, hatch, opacity: dim ? 0.22 : undefined }
    },
    [mode, slotOf, userIdx, dateRange, hl, lens],
  )

  // per-user roll-up for the current view: top users, plus the remainder that
  // isn't covered by the top-users list; $ figures use class-aware per-user
  // rates when the snapshot carries them
  const rollupFor = (node: TreeNode) => {
    const us = node.us ?? []
    if (!us.length) return []
    const userRate = (u: string) => pricing && (pricing.userRates?.[u] ?? pricing.blended)
    const rest = node.b - us.reduce((s, [, b]) => s + b, 0)
    return [
      ...us.map(([u, b]) => ({ k: u, b, col: userColor(u, userIdx), rate: userRate(u), mix: pricing?.userMix?.[u] })),
      ...(rest > 0 ? [{ k: '(other)', b: rest, col: 'var(--other)', rate: pricing?.blended, mix: undefined }] : []),
    ].sort((a, b) => b.b - a.b)
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
            {r.k.startsWith('(') || r.k === 'unattributed' ? r.k : <UserChip who={r.k} size={15} />} <b>{fmtBytes(r.b)}</b>
            <span className="pct">{((100 * r.b) / node.b).toFixed(1)}%</span>
            {r.rate != null && (
              r.mix ? (
                <Tooltip content={<ClassMixTip mix={r.mix} note="the user's fleet-wide class mix sets their $/byte rate; $ shown = rate × this view's bytes" />}>
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
     user mode is dropped (the roll-up already shows the same swatch+label,
     plus size and $). tree (prefix colors) and age (date gradient) convey
     distinct keys, so they keep the legend. */
  const legend = mode !== 'user'
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
    const gsPath = pathOf(path)
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
    const users = n.us && n.us.length > 0 && (
      <div className="users">
        {n.us.map(([u, b]) => (
          <div className="tt-user" key={u}>
            {mode === 'user' && <span className="sw" style={{ background: userColor(u, userIdx) }} />}
            <UserChip who={u} size={15} /> · {fmtBytes(b)}
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
        {users}
      </>
    )
  }

  return (
    <DtTreemap<TreeNode>
      root={root}
      initialPath={initialPath}
      tiling={tiling}
      getSize={n => n.b}
      getChildren={n => n.c}
      getLabel={n => n.n}
      getId={(_n, p) => pathOf(p)}
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
