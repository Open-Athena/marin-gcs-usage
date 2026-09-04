import { useCallback, useState } from 'react'
import { Treemap as DtTreemap } from '@disk-tree/react'
import type { CellCtx, CellStyle } from '@disk-tree/react'
import { dateColor, dateGradientCss, epochDaysToMonth, inkFor, slotColor, userColor } from './colors'
import type { UserIndex } from './colors'
import { ClassMixTip, Tooltip } from './Tooltip'
import type { ColorMode, Pricing, TreeNode } from './types'
import { CLASS_NAMES, classMix, fmtN, fmtUsd, ratePerByte } from './types'
import { UserChip } from './UserChip'
import { TilingToggle, useTiling } from './tiling'
import { useUnits } from './units'

// Tree-mode colouring is relative to the *drilled* root: its direct children
// (L1) take the distinct category hues, ranked by size — the macro axis — and
// each L1's own children (L2) fan across shades of that hue — the micro axis;
// deeper cells inherit their L2 ancestor's shade. Drilling re-keys both, so
// whatever you're looking at gets the full palette.
const MAX_SLOTS = 8
const rankCache = new WeakMap<TreeNode, Map<string, [number, number]>>()
/** name → [rank, count] over a node's real (non-fold) children, largest first. */
function childRanks(node: TreeNode): Map<string, [number, number]> {
  let m = rankCache.get(node)
  if (!m) {
    const kids = (node.c ?? []).filter(c => !c.n.startsWith('(')).sort((a, b) => b.b - a.b)
    m = new Map(kids.map((c, i): [string, [number, number]] => [c.n, [i, kids.length]]))
    rankCache.set(node, m)
  }
  return m
}

/** The `s3://…` path shown at the top of a pinned tooltip, with a copy-to-
 * clipboard button (eject the prefix to the CLI). Its own component so the copy
 * state has somewhere to live (renderTooltip is a plain function). */
function PathBar({ uri }: { uri: string }) {
  const [copied, setCopied] = useState(false)
  const slash = uri.lastIndexOf('/') + 1
  return (
    <div className="path">
      <span className="dirname">{uri.slice(0, slash)}</span>
      <span className="basename">{uri.slice(slash)}</span>
      <span className="path-acts" onClick={e => e.stopPropagation()}>
        <button
          type="button" className="path-copy" title="Copy path to clipboard"
          onClick={() => navigator.clipboard?.writeText(uri).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1200) })}
        >{copied ? 'copied ✓' : 'copy'}</button>
      </span>
    </div>
  )
}

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
  // Macro/micro hue for a cell, relative to the drilled root (see childRanks):
  // `depth` counts from the view root, so kidPath[len-2-depth] is that root,
  // the next entry the L1 category, the one after (if any) the L2 shade.
  const slotOf = useCallback(
    (kidPath: TreeNode[], depth: number): { slot: number; i: number; n: number } | null => {
      const rootIdx = kidPath.length - 2 - depth
      const viewRoot = kidPath[rootIdx]
      const l1 = kidPath[rootIdx + 1]
      if (!viewRoot || !l1 || l1.n.startsWith('(')) return null
      const slot = childRanks(viewRoot).get(l1.n)?.[0]
      if (slot == null || slot >= MAX_SLOTS) return null
      const l2 = kidPath[rootIdx + 2]
      const [i, n] = l2 && !l2.n.startsWith('(') ? childRanks(l1).get(l2.n) ?? [0, 1] : [0, 1]
      return { slot, i, n }
    },
    [],
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
    (kid: TreeNode, kidPath: TreeNode[], depth: number, ctx: CellCtx): CellStyle => {
      let bg: string
      let ink: string
      if (mode === 'tree') {
        const s = slotOf(kidPath, depth)
        bg = s ? slotColor(s.slot, s.i, s.n) : 'var(--other)'
        ink = s ? inkFor(bg) : 'var(--ink)'
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
        // user: a cell takes a user's color only when it is (nearly) wholly
        // theirs — mixed boxes stay gray until you drill/zoom. The bar is 94%
        // rather than ~100%: a dominant owner with a sub-6% remainder read as
        // unattributed gray (gcs `9fe605b`).
        const [u, ub] = kid.us?.[0] ?? [null, 0]
        bg = userColor(ub >= 0.94 * kid.b ? u : null, userIdx)
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
      // Shared-edge stroke, per cell: each neighbor paints its own half of a
      // boundary, so the line can adapt to the face it borders. Top-level
      // rects take the page background — the strongest seam the theme has —
      // and deeper cells pull their own fill toward it, so even grey-on-grey
      // siblings show a visible edge.
      const edge = depth === 0
        ? 'var(--surface)'
        : `color-mix(in oklab, ${bg} ${depth === 1 ? 40 : 62}%, var(--surface))`
      return { bg, ink, hatch, edge, opacity: dim ? 0.22 : undefined }
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
  const modeLegend = mode !== 'user'
    ? (legendNode: TreeNode, legendPath: TreeNode[]) => (
        <>
          {mode === 'date' && dateRange ? (
            <span className="li gradli">
              {epochDaysToMonth(dateRange.min)}
              <span className="gradbar" style={{ background: dateGradientCss() }} />
              {epochDaysToMonth(dateRange.max)}
            </span>
          ) : (
            // The macro axis: the drilled root's children, largest first, in
            // their hue; anything past the palette (and folds) is "other".
            (() => {
              const ranked = [...childRanks(legendNode).entries()].sort((a, b) => a[1][0] - b[1][0])
              const shown = ranked.slice(0, MAX_SLOTS)
              const hasOther = ranked.length > MAX_SLOTS || (legendNode.c ?? []).some(c => c.n.startsWith('('))
              return (
                <>
                  {shown.map(([k, [i]]) => (
                    <span className="li" key={k}>
                      <span className="sw" style={{ background: slotColor(i) }} />
                      {k}
                    </span>
                  ))}
                  {hasOther && <span className="li"><span className="sw" style={{ background: 'var(--other)' }} />other</span>}
                </>
              )
            })()
          )}
        </>
      )
    : null
  // The tiling toggle rides in the same slot (right of the crumbs, left of ⛶)
  // in every mode: a map-level preference belongs on the map, not in the nav.
  const legend = (legendNode: TreeNode, legendPath: TreeNode[]) => (
    <div className="legend">
      {modeLegend?.(legendNode, legendPath)}
      <TilingToggle />
    </div>
  )

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
        <PathBar uri={gsPath} />
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
      // Depth-emphasized seams: the core default (max(1, 3-depth)) tops out
      // at 1.5px painted per side — invisible between same-grey siblings.
      // Give the top level a fat gutter (3px per side → 6px between cells),
      // one step down a clear line, leaves a hairline. Colors come from
      // `colorForCell`'s `edge` (page-bg at depth 0, fill-adaptive below).
      // Capped by cell size: drilling into a flat dir puts hundreds of small
      // cells at depth 0, where the 6px seam eats the area shared-edges mode
      // exists to preserve.
      borderWidth={(depth, { w, h }) => {
        const base = depth === 0 ? 6 : depth === 1 ? 2.5 : 1
        return Math.min(base, Math.max(1, Math.min(w, h) / 16))
      }}
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
