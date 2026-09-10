import { useEffect, useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import { intParam, useUrlState } from 'use-prms'
import { useCanMark } from './auth'
import { dateColor, epochDaysToDate, epochDaysToMonthShort } from './colors'
import type { UserIndexEntry } from './colors'
import { ACTION_COLORS, KEEP_TIP, KLC_TIP, SWEEP_TIP, clearTip } from './MarkControls'
import type { MarkAction, MarkIndex } from './marks'
import { ACTION_LABELS, useMarkMutations } from './marks'
import { OwnerBar, ownerShares } from './OwnerBar'
import { markAllowed, looksCkpt, subtreeStateTotals } from './sweep'
import type { MarkState, MarkAxis, KlcIndex } from './sweep'
import { Tooltip } from './Tooltip'
import { elideMid } from './CopyName'
import { AssignSelect } from './AssignSelect'
import { OwnerFactChip } from './OwnerFactChip'
import { useRowSelection, useRowSelectionKeys } from './rowSelection'
import type { TreeNode } from './types'
import { fmtN } from './types'
import { useUnits } from './units'

// Sortable, paged listing of the treemap's current node's children — the
// tabular twin of the map above it (same drill: clicking a row opens it).
// Row selection + bulk marking: specs/children-table-selection.md.

type SortKey = 'n' | 'b' | 'o' | 'd' | 'a'

// Rows per page: `?n=` (default 20); the pager offers the usual sizes.
const PAGE_SIZES = [20, 50, 100, 200]

/** Names longer than this elide from the middle (the full path is in the
 *  tooltip); ~60 chars fills the column's 480px at 12px mono. */
const NAME_MAX = 60

export function ChildrenTable({ node, segs, scheme, markIdx, klcIdx, states, userIdx, onPickUser, onOpen }: {
  /** The treemap's currently-viewed node. */
  node: TreeNode
  /** Path segments from the tree root to `node` (no scheme, no root). */
  segs: string[]
  scheme: string
  markIdx?: MarkIndex | null
  /** KLC splits, so a keep-last-ckpt subtree's bytes settle into real keep / sweep. */
  klcIdx?: KlcIndex
  /** The page's mark-state axis: list only children whose effective decision
   * is in it (`{unmarked}` = the old To-do lens). Absent = every child. */
  states?: ReadonlySet<MarkAxis> | null
  userIdx?: Map<string, UserIndexEntry>
  onPickUser?: (u: string) => void
  onOpen: (segs: string[]) => void
}) {
  const { fmtBytes } = useUnits()
  const { put, post } = useMarkMutations()
  const canMark = useCanMark()
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'b', asc: false })
  const [page, setPage] = useState(0)
  const [nP, setNP] = useUrlState('n', intParam(20))
  const PAGE = PAGE_SIZES.includes(nP) ? nP : 20
  const showActions = !!markIdx && canMark
  const mark = (uri: string, action: MarkAction | null) => put.mutate({ prefix: uri + '/', action })

  const kids = useMemo(() => {
    let ks = (node.c ?? []).slice()
    // Mark axis: keep only real children whose effective decision is in it.
    if (states && markIdx) {
      ks = ks.filter(k => !k.n.startsWith('(') && markAllowed(markIdx.resolve(scheme + [...segs, k.n].join('/')).mark?.action ?? 'unmarked', states))
    }
    const dir = sort.asc ? 1 : -1
    const val = (n: TreeNode): number | string =>
      sort.k === 'n' ? n.n
      : sort.k === 'b' ? n.b
      : sort.k === 'o' ? n.o
      : sort.k === 'a' ? n.a ?? -Infinity
      : n.d ?? -Infinity
    return ks.sort((a, b) => {
      const va = val(a)
      const vb = val(b)
      return (typeof va === 'string' ? (va as string).localeCompare(vb as string) : (va as number) - (vb as number)) * dir
    })
  }, [node, sort, states, markIdx, scheme, segs])
  // A new listing (drill, sort, lens) starts on page 1.
  useEffect(() => setPage(0), [node, sort, states])

  // Created-month ink: an age gradient over the listed rows' range, so a
  // column of "May / Jun / Apr" also reads at a glance as older ↔ newer.
  const [dMin, dMax] = useMemo(() => {
    const ds = kids.map(k => k.d).filter((d): d is number => d != null)
    return ds.length ? [Math.min(...ds), Math.max(...ds)] : [0, 0]
  }, [kids])
  const ageInk = (d: number) => dateColor(dMax > dMin ? (d - dMin) / (dMax - dMin) : 1)

  if (!kids.length) {
    return states
      ? <section className="children-tbl"><p className="tab-note">No prefix under this view is {[...states].join(' / ')}.</p></section>
      : null
  }
  const th = (k: SortKey, label: string, num = true) => (
    <th
      className={(num ? 'num ' : '') + 'sortable' + (sort.k === k ? ' on' : '')}
      onClick={() => setSort(s => ({ k, asc: s.k === k ? !s.asc : k === 'n' }))}
      title="sort"
    >
      {label}{sort.k === k ? (sort.asc ? ' ▲' : ' ▼') : ''}
    </th>
  )
  const pages = Math.max(1, Math.ceil(kids.length / PAGE))
  const pg = Math.min(page, pages - 1)
  const shown = kids.slice(pg * PAGE, (pg + 1) * PAGE)
  const uriOfKid = (k: TreeNode) => scheme + [...segs, k.n].join('/')
  // Rows select (click / shift / ⌘, checkboxes, j/k) into one set keyed by
  // uri; the bar above the table marks or assigns the whole selection at once.
  const selectable = shown.filter(k => !k.n.startsWith('('))
  const sel = useRowSelection(selectable, uriOfKid)
  useRowSelectionKeys(sel, 'tbl', 'Children table')
  // Selection survives paging and sort by key, but not a drill: the rows
  // picked under one path aren't candidates for a bulk mark under another.
  const path = segs.join('/')
  const clearSel = sel.clear
  useEffect(() => clearSel(), [path, clearSel])
  const selUris = [...sel.selected]
  const bulkMark = (action: MarkAction | null) => { if (selUris.length) post.mutate(selUris.map(u => ({ pattern: u + '/', keep: action })), { onSuccess: () => sel.clear() }) }
  const selBytes = kids.filter(k => sel.selected.has(uriOfKid(k))).reduce((s, k) => s + k.b, 0)
  const selBar = showActions && sel.selected.size > 0 && (
    <span className="sel-bar">
      <b>{sel.selected.size}</b> selected · {fmtBytes(selBytes)}
      <span className="acts">
        <span className="lbl">mark all</span>
        {(['keep', 'sweep', 'keep_last_ckpt'] as MarkAction[]).map(a => (
          <Tooltip content={<>Mark every selected prefix <b>{ACTION_LABELS[a]}</b> (one batched save)</>} key={a}>
            <button type="button" className={`dot ${a}`} style={{ ['--act' as string]: ACTION_COLORS[a] }} onClick={() => bulkMark(a)} aria-label={ACTION_LABELS[a]} />
          </Tooltip>
        ))}
        <Tooltip content="Clear the marks on every selected prefix (back to undecided)">
          <button type="button" className="dot clear" onClick={() => bulkMark(null)} aria-label="clear marks">×</button>
        </Tooltip>
        <AssignSelect prefix={selUris.map(u => u + '/')} label={`assign ${sel.selected.size}…`} />
        <button type="button" className="quiet" onClick={sel.clear}>deselect</button>
      </span>
    </span>
  )
  const pager = pages > 1 && (
    <span className="pg">
      <button type="button" disabled={pg === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
      <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous page">‹</button>
      <span>{pg * PAGE + 1}–{Math.min(kids.length, (pg + 1) * PAGE)} of {kids.length.toLocaleString('en-US')}</span>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label="next page">›</button>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
      <select className="psize" value={PAGE} onChange={e => { setNP(+e.target.value); setPage(0) }} aria-label="rows per page">
        {PAGE_SIZES.map(n => <option key={n} value={n}>{n} / page</option>)}
      </select>
    </span>
  )
  // The top bar holds the pager on the left and the selection on the right;
  // it's always there once rows are selectable, so a selection appearing
  // doesn't push the row that was just clicked out from under the pointer.
  // A click on the section's own dead space (not a row / control) deselects.
  const clearOnDeadClick = (e: MouseEvent) => {
    if (sel.selected.size && !(e.target as HTMLElement).closest('tr, button, input, select, a, .sel-bar')) sel.clear()
  }
  // One small dot per decision, colored by state: filled = this row's OWN
  // mark, dashed = the mark it inherits from above, hollow = available.
  const dot = (uri: string, a: MarkAction, st: 'own' | 'inh' | null, tip: string) => (
    <Tooltip content={<>{st === 'own' ? 'Marked ' : st === 'inh' ? 'Inherits ' : 'Mark '}<b>{ACTION_LABELS[a]}</b>{st === 'inh' ? ' from a directory above (click to set it here)' : ''}<div className="how">{tip}</div></>} key={a}>
      <button type="button" className={`dot ${a}${st === 'own' ? ' on' : st === 'inh' ? ' inh' : ''}`} style={{ ['--act' as string]: ACTION_COLORS[a] }} onClick={() => mark(uri, a)} aria-label={ACTION_LABELS[a]} />
    </Tooltip>
  )
  // MarkState of the bytes UNDER a row: keep / last-ckpt / sweep / undecided, as a
  // bar — a directory is rarely one thing (an inherited keep with swept
  // subtrees, a KLC with its kept step), and a single word hid that.
  const STATE_COLORS: Record<MarkState, string> = { keep: ACTION_COLORS.keep, keep_last_ckpt: ACTION_COLORS.keep_last_ckpt, sweep: ACTION_COLORS.sweep, unmarked: 'var(--other)' }
  const STATE_LABELS: Record<MarkState, string> = { keep: 'keep', keep_last_ckpt: 'last ckpt', sweep: 'sweep', unmarked: 'undecided' }
  const stateBar = (k: TreeNode, uri: string) => {
    if (!markIdx || !k.b) return <span className="none">—</span>
    const f = subtreeStateTotals(k, uri, markIdx, klcIdx)
    const parts = (Object.keys(f) as MarkState[]).filter(x => f[x] > 0)
    return (
      <Tooltip content={
        <span className="own-tip">
          <div>Bytes under this directory by decision:</div>
          {parts.map(x => <div className="row" key={x}><i style={{ background: STATE_COLORS[x] }} />{STATE_LABELS[x]}<span className="n">{fmtBytes(f[x])} · {Math.round((100 * f[x]) / k.b)}%</span></div>)}
        </span>
      }>
        <span className="own-bar state-bar" style={{ width: 70 }} aria-label="marks distribution">
          {parts.map(x => <i key={x} style={{ width: `${(100 * f[x]) / k.b}%`, background: STATE_COLORS[x] }} />)}
        </span>
      </Tooltip>
    )
  }
  return (
    <section className="children-tbl" onClick={clearOnDeadClick}>
      {(pager || showActions) && <div className="pager top">{pager}{selBar}</div>}
      <table className="worklist selectable">
        <thead>
          <tr>
            {showActions && <th className="col-sel"><input type="checkbox" title="select / deselect this page (⇧x)" checked={sel.pageAll} onChange={sel.togglePage} /></th>}
            {th('n', 'name', false)}
            {th('b', 'bytes')}
            <th className="num">share</th>
            {th('o', 'objects')}
            {th('d', 'created')}
            {th('a', 'read', false)}
            <th>owner(s)</th>
            {markIdx && <th>marks</th>}
            {showActions && <th>actions</th>}
          </tr>
        </thead>
        <tbody>
          {shown.map(k => {
            const synthetic = k.n.startsWith('(')
            const kidSegs = [...segs, k.n]
            const uri = scheme + kidSegs.join('/')
            const shares = ownerShares(k)
            const cl = markIdx && !synthetic ? markIdx.claimOf(uri) : null
            const mk = markIdx && !synthetic ? markIdx.resolve(uri) : null
            const si = selectable.indexOf(k)
            return (
              <tr key={k.n} ref={si >= 0 ? sel.rowRef(si) : undefined} {...(si >= 0 && showActions ? sel.rowProps(si) : {})}>
                {showActions && <td className="col-sel">{!synthetic && <input type="checkbox" checked={sel.isSelected(k)} onChange={() => sel.toggle(si)} />}</td>}
                <td className="prefix">
                  <Tooltip content={<code className="elide-full">{uri}</code>}>
                    {synthetic || !k.c?.length ? (
                      <span>{elideMid(k.n, NAME_MAX)}</span>
                    ) : (
                      <a role="link" tabIndex={0} onClick={() => onOpen(kidSegs)}>{elideMid(k.n, NAME_MAX)}</a>
                    )}
                  </Tooltip>
                </td>
                <td className="num">{fmtBytes(k.b)}</td>
                <td className="num">{node.b ? ((100 * k.b) / node.b).toFixed(1) : 0}%</td>
                <td className="num">{fmtN(k.o)}</td>
                <td className="created num">{k.d != null ? (() => {
                  const [mon, yr] = epochDaysToMonthShort(k.d).split(' ')
                  return <span className="cm"><i style={{ background: ageInk(k.d) }} /><span className="mon">{mon}</span><span className="yr">{yr ?? ''}</span></span>
                })() : '—'}</td>
                <td title={k.a != null ? 'most recent GET/HEAD/LIST under this prefix (access logs)' : undefined}>
                  {k.a != null ? epochDaysToDate(k.a) : '—'}
                </td>
                {/* An assignee, or a single attributed owner, by name; a mix
                    as a bar (names and shares on hover). */}
                <td className="owners">
                  {cl ? <OwnerFactChip who={cl.who} assigned={{ by: cl.by, ts: cl.ts, memo: cl.memo }} />
                    : shares.length === 1 && shares[0][1] >= 0.98 * k.b ? <OwnerFactChip who={shares[0][0]} inferred={k.pv ?? null} />
                    // Picking a person from a ROW's bar means "this directory,
                    // theirs only": drill into the row, then apply the lens.
                    : shares.length ? <OwnerBar node={k} userIdx={userIdx} width={70} onPickUser={onPickUser && !synthetic ? u => { onOpen(kidSegs); onPickUser(u) } : undefined} />
                    : <span className="none">—</span>}
                </td>
                {markIdx && <td className="state">{synthetic ? <span className="none">—</span> : stateBar(k, uri)}</td>}
                {showActions && (
                  <td className="actions">
                    {synthetic ? null : (
                      <>
                        {dot(uri, 'keep', mk?.mark?.action === 'keep' ? (mk.own ? 'own' : 'inh') : null, KEEP_TIP)}
                        {dot(uri, 'sweep', mk?.mark?.action === 'sweep' ? (mk.own ? 'own' : 'inh') : null, SWEEP_TIP)}
                        {/* Last-ckpt keeps its column whether or not it's offered, so
                            the row of dots doesn't shift between rows. */}
                        {looksCkpt(k, uri) ? dot(uri, 'keep_last_ckpt', mk?.mark?.action === 'keep_last_ckpt' ? (mk.own ? 'own' : 'inh') : null, KLC_TIP) : <span className="dot-gap" />}
                        <span className="tail">
                          {mk?.own && (
                            <Tooltip content={clearTip(true)}>
                              <button type="button" className="dot clear" onClick={() => mark(uri, null)} aria-label="clear mark">×</button>
                            </Tooltip>
                          )}
                          <AssignSelect prefix={uri + '/'} assigned={cl?.who ?? null} compact />
                        </span>
                      </>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          {/* Totals of the LISTED rows — under a scoping lens (to-do) this is
              the lens total, not the parent node's. */}
          <tr className="total-row">
            {showActions && <td />}
            <td>total{kids.length !== (node.c ?? []).length ? ` (${kids.length} shown)` : ''}</td>
            <td className="num">{fmtBytes(kids.reduce((s, k) => s + k.b, 0))}</td>
            <td className="num">{node.b ? ((100 * kids.reduce((s, k) => s + k.b, 0)) / node.b).toFixed(1) : 0}%</td>
            <td className="num">{kids.reduce((s, k) => s + k.o, 0).toLocaleString('en-US')}</td>
            <td colSpan={4 + (markIdx ? 1 : 0) + (showActions ? 2 : 0)} />
          </tr>
        </tfoot>
      </table>
      {pager && <div className="pager">{pager}</div>}
    </section>
  )
}
