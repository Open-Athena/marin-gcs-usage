import { useCallback, useMemo } from 'react'
import { Treemap as DtTreemap } from '@disk-tree/react'
import type { CellCtx, CellStyle } from '@disk-tree/react'
import { Avatar } from './Avatar'
import { ghHandle, shortName } from './UserChip'
import { dateColor, dateGradientCss, epochDaysToDate, epochDaysToMonth, inkFor, slotColor, userColor } from './colors'
import type { UserIndexEntry } from './colors'
import { ACTION_COLORS, MarkControls, markProvenance } from './MarkControls'
import { ACTION_LABELS } from './marks'
import type { MarkIndex } from './marks'
import { ClassMixTip, Tooltip } from './Tooltip'
import type { ColorMode, Pricing, TreeNode } from './types'
import { CLASS_NAMES, TEAM_VARS, classMix, domTeamSeg, fmtN, fmtUsd, groupLabel, ratePerByte, sharedColor } from './types'
import { useUnits } from './units'

// A top-level prefix holding more than this share of the store is split one
// level deeper for colouring (see catSlot).
const DOMINANT_FRAC = 0.4
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
// Scale a group's fleet-wide class-byte mix down to `b` bytes, so the rollup
// tooltip's table totals the $ figure it explains (rate × this view's bytes)
// rather than showing the group's whole-fleet Ti/$ next to a small slice.
const scaleMix = (mix: Record<string, number>, b: number): Record<string, number> => {
  const tot = Object.values(mix).reduce((s, x) => s + x, 0)
  return tot ? Object.fromEntries(Object.entries(mix).map(([c, x]) => [c, (x * b) / tot])) : mix
}

export function Treemap({ root, mode, userIdx, dateRange, readRange, hl, pricing, lens, scheme = 'gs://', redact, markIdx, initialPath, path, onPathChange }: {
  root: TreeNode
  mode: ColorMode
  userIdx: Map<string, UserIndexEntry>
  dateRange: DateRange | null
  // Access-log observation window (epoch days) — domain of the read lens.
  readRange?: DateRange | null
  hl?: Highlight | null
  pricing?: Pricing | null
  lens?: boolean
  // URI scheme for cell paths — `gs://` for GCS, `s3://` for CoreWeave.
  scheme?: string
  // OG-image mode: hide every text detail (cell labels, crumb/rollup bars, hint)
  // and render just the colored cells. Never set by the live app.
  redact?: boolean
  // Mark & sweep mode (/mark): overlay keep/delete badges and marking controls.
  markIdx?: MarkIndex | null
  // Start drilled here (e.g. CW's lone bucket) — crumbs keep the ancestry.
  initialPath?: TreeNode[]
  // Controlled drill path + change reporting (upstream contract) — lets the
  // app keep the drill in the URL and command drills from worklist rows.
  path?: TreeNode[]
  onPathChange?: (p: TreeNode[]) => void
}) {
  const { fmtBytes } = useUnits()
  // Fixed category colors: global top-level dirs by total size. A single-bucket
  // store can be lopsided enough that one prefix owns most of the map (`marin/`
  // is ~87% of the CoreWeave bucket), which paints almost every cell the same
  // hue — so any prefix over DOMINANT_FRAC hands its slot down to its own
  // children, and they get the distinct hues instead.
  const { catSlot, splitCats, hueIdx } = useMemo(() => {
    const tops: TreeNode[] = []
    for (const bucket of root.c ?? []) tops.push(...(bucket.c ?? []))
    const nameOf = (n: TreeNode) => (n.n.startsWith('(') ? '(other)' : n.n)
    const total = root.b || 1
    const splitCats = new Set(
      tops.filter(d => nameOf(d) !== '(other)' && d.b / total > DOMINANT_FRAC).map(nameOf),
    )
    const bytes = new Map<string, number>()
    const add = (k: string, b: number) => bytes.set(k, (bytes.get(k) ?? 0) + b)
    for (const d of tops) {
      const k = nameOf(d)
      if (splitCats.has(k)) for (const c of d.c ?? []) add(nameOf(c) === '(other)' ? '(other)' : `${k}/${c.n}`, c.b)
      else add(k, d.b)
    }
    const cats = [...bytes.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([k]) => k)
      .filter(k => k !== '(other)')
    const catSlot = new Map(cats.slice(0, 8).map((k, i): [string, number] => [k, i]))

    // Rank each category's own children by size, so the hue fan is stable and
    // orders large→small rather than by whatever order the tree happens to be
    // in. Keyed by the child's full path so lookup from `kidPath` is exact —
    // bare names collide (`store` appears under several prefixes).
    const hueIdx = new Map<string, [number, number]>()
    const rank = (catNode: TreeNode, prefix: string) => {
      const kids = (catNode.c ?? []).filter(c => !c.n.startsWith('('))
      const sorted = [...kids].sort((a, b) => b.b - a.b)
      sorted.forEach((c, i) => hueIdx.set(`${prefix}/${c.n}`, [i, sorted.length]))
    }
    for (const bucket of root.c ?? []) {
      for (const d of bucket.c ?? []) {
        const k = nameOf(d)
        if (splitCats.has(k)) for (const c of d.c ?? []) rank(c, `${bucket.n}/${k}/${c.n}`)
        else rank(d, `${bucket.n}/${k}`)
      }
    }
    return { catSlot, splitCats, hueIdx }
  }, [root])

  const slotOf = useCallback(
    (kidPath: TreeNode[]): { slot: number; i: number; n: number } | null => {
      // kidPath: [root, bucket, d1, d2, …]
      const top = kidPath[2]
      if (!top) return null
      const k = top.n.startsWith('(') ? '(other)' : top.n
      const split = splitCats.has(k)
      let slot: number | undefined
      if (!split) slot = catSlot.get(k)
      else {
        const sub = kidPath[3]
        slot = sub ? catSlot.get(sub.n.startsWith('(') ? '(other)' : `${k}/${sub.n}`) : undefined
      }
      if (slot == null) return null
      // The node one level below whichever node owns the slot carries the hue
      // offset; everything under it inherits that offset unchanged.
      const catDepth = split ? 3 : 2
      const hueNode = kidPath.slice(1, catDepth + 2)
      const [i, n] = hueIdx.get(hueNode.map(x => x.n).join('/')) ?? [0, 1]
      return { slot, i, n }
    },
    [catSlot, splitCats, hueIdx],
  )

  const uriOf = (path: TreeNode[]) => scheme + path.slice(1).map(n => n.n).join('/')

  // Fold merger: first-class TreeNode aggregating tm/us/d so folded tiles keep
  // real tooltips (upstream calls this at every nesting level).
  const mergeSmall = useCallback((tiny: TreeNode[]): TreeNode => {
    const b = tiny.reduce((s, it) => s + it.b, 0)
    const o = tiny.reduce((s, it) => s + it.o, 0)
    const tm: Record<string, number> = {}
    const us: Record<string, number> = {}
    let wd = 0
    let wdb = 0
    let ma = -1
    for (const it of tiny) {
      for (const [t, tb] of Object.entries(it.tm ?? {})) tm[t] = (tm[t] ?? 0) + tb
      for (const [u, ub] of it.us ?? []) us[u] = (us[u] ?? 0) + ub
      if (it.d != null) {
        wd += it.d * it.b
        wdb += it.b
      }
      if (it.a != null && it.a > ma) ma = it.a
    }
    const folded: TreeNode = { n: `(+${tiny.length})`, b, o }
    if (wdb) folded.d = Math.round(wd / wdb)
    if (ma >= 0) folded.a = ma
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
        const s = slotOf(kidPath)
        if (!s && ctx.hasKids) {
          // A container with no slot of its own — most importantly the split
          // prefix itself (`marin/`, whose colour lives on its children). Cells
          // are translucent, so painting it "(other)" grey would show through
          // every child and mute them; stay neutral and let the kids carry it.
          bg = 'var(--panel)'
          ink = 'var(--ink)'
        } else {
          bg = s ? slotColor(s.slot, s.i, s.n) : 'var(--other)'
          ink = s ? inkFor(bg) : 'var(--ink)'
        }
      } else if (mode === 'fate') {
        // Keep-axis fate: paint kept (green) / keep-last-ckpt / swept (red);
        // undecided cells stay grey — the review to-do, visible at a glance.
        const st = markIdx?.resolve(uriOf(kidPath))
        const m = st?.mark ?? null
        if (m && (st!.own || !ctx.hasKids)) {
          bg = ACTION_COLORS[m.action]
          ink = inkFor(bg)
        } else if (ctx.hasKids) {
          bg = 'var(--panel)' // container without its own mark: children carry the fate
          ink = 'var(--ink)'
        } else {
          bg = 'var(--other)' // undecided leaf
          ink = 'var(--ink)'
        }
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
      } else if (mode === 'read') {
        if (kid.a != null && readRange && readRange.max > readRange.min) {
          bg = dateColor((kid.a - readRange.min) / (readRange.max - readRange.min))
          ink = inkFor(bg)
        } else {
          // Never read since logging began — the sweep-interesting bucket.
          bg = 'var(--never-read)'
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
    [mode, slotOf, userIdx, dateRange, readRange, hl, lens, markIdx],
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
      const userTotal = us.reduce((s, [, b]) => s + b, 0)
      // group/communal bytes with no individual owner (exact once `us` is
      // uncapped; with legacy top-5 snapshots the tail users land here too)
      const shared = attributed - userTotal
      // individual traces while they stay legible: everyone ≥1% of the node,
      // at least 5, at most 12; the rest roll into "(other users)"
      const shown = us.filter(([, b], i) => i < 5 || (i < 12 && b >= 0.01 * node.b))
      const otherUsers = userTotal - shown.reduce((s, [, b]) => s + b, 0)
      return [
        ...shown.map(([u, b]) => ({ k: u, b, col: userColor(u, userIdx, mode === 'uteam'), rate: userRate(u), mix: pricing?.userMix?.[u] })),
        ...(otherUsers > 0 ? [{ k: `(other users ×${us.length - shown.length})`, b: otherUsers, col: 'var(--other)', rate: pricing?.blended, mix: undefined }] : []),
        ...(shared > 0 ? [{ k: '(shared/communal)', b: shared, col: 'var(--shared)', rate: pricing?.blended, mix: undefined }] : []),
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

  // Mark decoration is state-as-*border* (keep green / keep-last-ckpt amber /
  // sweep red), NOT an ✕ stamped on every descendant. A marked prefix inherits
  // to its whole subtree, so decorating every cell is redundant noise — we mark
  // only the top-most rendered cell that carries each mark:
  //   - `own`  (the mark sits exactly here), or
  //   - the drill root's direct children (a mark at/above the root surfaces here
  //     first; its deeper descendants share it and stay undecorated).
  // Skipped in `fate` mode, where the fill already *is* the fate. Bigger cells
  // also get a corner badge: the actor's avatar + the state glyph. Full-opacity
  // (inherited marks are no longer faded — they read as active). Provenance
  // (who/when/inherited-from) lives in the cell tooltip.
  const drillDepth = (path ?? initialPath)?.length ?? 1
  const renderCellExtra = markIdx
    ? (n: TreeNode, cellPath: TreeNode[], { w, h }: { w: number; h: number }) => {
        if (mode === 'fate') return null
        // Folded "(other)" tiles reuse their *parent's* path, so the top-level
        // fold has the drill root's path length. Decorate only that one — it
        // shares its siblings' fate and should share their border, not read as
        // an un-bordered gap; nested folds stay clean.
        const isFold = n.n.startsWith('(')
        if (isFold && cellPath.length !== drillDepth) return null
        const { mark, own } = markIdx.resolve(uriOf(cellPath))
        if (!mark) return null
        const topLevel = cellPath.length === drillDepth + 1 || isFold
        if (!own && !topLevel) return null
        const color = ACTION_COLORS[mark.action]
        const glyph = mark.action === 'keep' ? '✓' : mark.action === 'keep_last_ckpt' ? '◐' : '✕'
        // Low floor so a *thin* top-level tile still reads as marked (a bare
        // sliver among bordered siblings looked like a gap); on a very narrow
        // cell the inset border just fills it with the fate color.
        const border = w >= 4 && h >= 5
          ? <span className="mark-edge" style={{ borderColor: color }} /> : null
        // No actor badge on an aggregate fold — it's many prefixes, not one.
        const badge = !isFold && w >= 44 && h >= 24 ? (
          <span className="mark-badge">
            <Avatar github={ghHandle(mark.who)} name={shortName(mark.who)} size={14} />
            <span className="mk" style={{ background: color }}>{glyph}</span>
          </span>
        ) : null
        if (!border && !badge) return null
        return <>{border}{badge}</>
      }
    : undefined

  const renderRollup = (node: TreeNode, path: TreeNode[]) => {
    if (redact) return null
    const rollup = rollupFor(node)
    if (!markIdx && !rollup.length) return null
    return (
      <>
        {markIdx && <MarkControls uri={uriOf(path)} idx={markIdx} node={node} />}
        {rollup.filter(r => r.b >= 0.001 * node.b).map(r => {
          // Real per-user rows (user modes, not synthetic "(shared)"/"unattributed")
          // get a GitHub avatar next to the color swatch.
          const isUser = (mode === 'user' || mode === 'uteam') && !r.k.startsWith('(') && r.k !== 'unattributed'
          return (
          <span className="ri" key={r.k}>
            <span className="sw" style={{ background: r.col }} />
            {isUser && <Avatar github={ghHandle(r.k)} name={shortName(r.k)} size={15} />}
            {isUser ? shortName(r.k) : r.k} <b>{fmtBytes(r.b)}</b>
            <span className="pct">{((100 * r.b) / node.b).toFixed(1)}%</span>
            {r.rate != null && (
              r.mix ? (
                <Tooltip content={<ClassMixTip mix={scaleMix(r.mix, r.b)} note="assumes this slice mirrors the group's fleet-wide class mix — the table is that mix scaled to this view's bytes" />}>
                  <span className="usd dotted">{fmtUsd(r.b * r.rate)}/mo</span>
                </Tooltip>
              ) : (
                <span className="usd">{fmtUsd(r.b * r.rate)}/mo</span>
              )
            )}
          </span>
        )})}
      </>
    )
  }

  /* Legend only for modes where it isn't a strict subset of the roll-up bar:
     team + user modes are dropped (the roll-up already shows the same
     swatch+label, plus size and $). tree (prefix colors), age, and read
     (date gradients) convey distinct keys, so they keep the legend. */
  const legend = mode !== 'team' && mode !== 'user' && mode !== 'uteam'
    ? () => (
        <div className="legend">
          {mode === 'read' && readRange ? (
            <>
              <span className="li"><span className="sw" style={{ background: 'var(--never-read)' }} />never read</span>
              <span className="li gradli">
                {epochDaysToDate(readRange.min)}
                <span className="gradbar" style={{ background: dateGradientCss() }} />
                {epochDaysToDate(readRange.max)}
              </span>
            </>
          ) : mode === 'date' && dateRange ? (
            <span className="li gradli">
              {epochDaysToMonth(dateRange.min)}
              <span className="gradbar" style={{ background: dateGradientCss() }} />
              {epochDaysToMonth(dateRange.max)}
            </span>
          ) : mode === 'fate' ? (
            // Keep-axis fate: the cell fill IS the mark decision, so the legend
            // keys keep/last-ckpt/sweep/undecided — not the prefix colors below.
            <>
              <span className="li"><span className="sw" style={{ background: ACTION_COLORS.keep }} />{ACTION_LABELS.keep}</span>
              <span className="li"><span className="sw" style={{ background: ACTION_COLORS.keep_last_ckpt }} />{ACTION_LABELS.keep_last_ckpt}</span>
              <span className="li"><span className="sw" style={{ background: ACTION_COLORS.sweep }} />{ACTION_LABELS.sweep}</span>
              <span className="li"><span className="sw" style={{ background: 'var(--other)' }} />undecided</span>
            </>
          ) : (
            <>
              {[...catSlot.entries()].map(([k, s]) => (
                <span className="li" key={k}>
                  <span className="sw" style={{ background: slotColor(s) }} />
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
    const uri = uriOf(path)
    const userMode = mode === 'user' || mode === 'uteam'
    // The mark decision covering this cell — so provenance (who/when, inherited
    // or own) is always legible in the tooltip, even on cells too small for the
    // corner badge or when not in fate coloring.
    const st = markIdx && !n.n.startsWith('(') ? markIdx.resolve(uri) : null
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
            <Avatar github={ghHandle(u)} name={shortName(u)} size={13} /> {shortName(u)} · {fmtBytes(b)}
          </div>
        ))}
      </div>
    )
    return (
      <>
        <div className="path">
          <span className="dirname">{uri.slice(0, uri.lastIndexOf('/') + 1)}</span>
          <span className="basename">{uri.slice(uri.lastIndexOf('/') + 1)}</span>
        </div>
        <div className="nums">
          {fmtBytes(n.b)} · {fmtN(n.o)} objects · {((100 * n.b) / root.b).toFixed(2)}% of total
          {n.d != null && <> · mean created {epochDaysToMonth(n.d)}</>}
          {n.a != null
            ? <> · last read {epochDaysToDate(n.a)}</>
            : readRange && !n.n.startsWith('(') && <> · <span className="never-read">no reads since {epochDaysToDate(readRange.min)}</span></>}
        </div>
        {st?.mark && <div className="tt-mark">{markProvenance(st.mark, st.own)}</div>}
        {classes}
        {userMode ? <>{users}{teams}</> : <>{teams}{users}</>}
        {/* interactive only when the tooltip is pinned; CSS hides it on hover */}
        {markIdx && !n.n.startsWith('(') && <MarkControls uri={uriOf(path)} idx={markIdx} node={n} />}
      </>
    )
  }

  return (
    <DtTreemap<TreeNode>
      root={root}
      initialPath={initialPath}
      path={path}
      onPathChange={onPathChange}
      getSize={n => n.b}
      getChildren={n => n.c}
      getLabel={n => n.n}
      getId={(_n, p) => uriOf(p)}
      formatSize={fmtBytes}
      collapseChains
      mergeSmall={mergeSmall}
      colorForCell={colorForCell}
      // Opaque cells. Upstream's default fades every nesting level by 0.82,
      // which compounds: this store nests 6+ deep under `marin/datakit/...`,
      // so leaves landed near 0.4 alpha and every category washed out to the
      // same pale grey-blue. Structure comes from borders instead (app.scss).
      depthFade={1}
      rootFade={1}
      renderTooltip={renderTooltip}
      renderCellExtra={renderCellExtra}
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
      // Per-store style hook: deliberate CW/GCS presentation differences live
      // under these classes in app.scss (one codebase, no branches).
      className={`treemap store-${scheme === 's3://' ? 'cw' : 'gcs'}`}
    />
  )
}
