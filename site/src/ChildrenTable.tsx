import { useMemo, useState } from 'react'
import { useCanMark } from './auth'
import { epochDaysToDate, epochDaysToMonth } from './colors'
import { ACTION_COLORS, CLAIM_TIP, KEEP_TIP, KLC_TIP, SWEEP_TIP, clearTip, markProvenance } from './MarkControls'
import type { MarkAction, MarkIndex } from './marks'
import { ACTION_LABELS, useMarkMutations } from './marks'
import { looksCkpt } from './sweep'
import { Tooltip } from './Tooltip'
import type { TreeNode } from './types'
import { domTeamSeg, fmtN, groupLabel } from './types'
import { useUnits } from './units'

// Sortable/paginated listing of the treemap's current node's children — the
// tabular twin of the map above it (same drill: clicking a row opens it).

type SortKey = 'n' | 'b' | 'o' | 'd' | 'a'

const PAGE = 50

export function ChildrenTable({ node, segs, scheme, markIdx, todoOnly = false, onOpen }: {
  /** The treemap's currently-viewed node. */
  node: TreeNode
  /** Path segments from the tree root to `node` (no scheme, no root). */
  segs: string[]
  scheme: string
  markIdx?: MarkIndex | null
  /** To-do lens: drop children already settled by a keep/sweep decision. */
  todoOnly?: boolean
  onOpen: (segs: string[]) => void
}) {
  const { fmtBytes } = useUnits()
  const { put, claim } = useMarkMutations()
  const canMark = useCanMark()
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'b', asc: false })
  const [limit, setLimit] = useState(PAGE)
  const showActions = !!markIdx && canMark
  const mark = (uri: string, action: MarkAction | null) => put.mutate({ prefix: uri + '/', action })

  const kids = useMemo(() => {
    let ks = (node.c ?? []).slice()
    // To-do: keep only real children with no covering keep/sweep decision.
    if (todoOnly && markIdx) {
      ks = ks.filter(k => !k.n.startsWith('(') && !markIdx.resolve(scheme + [...segs, k.n].join('/')).mark)
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
  }, [node, sort, todoOnly, markIdx, scheme, segs])

  if (!kids.length) {
    return todoOnly
      ? <section className="children-tbl"><p className="tab-note">Nothing untriaged here — every prefix under this view has a keep/sweep decision.</p></section>
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
  const shown = kids.slice(0, limit)
  return (
    <section className="children-tbl">
      <table className="worklist">
        <thead>
          <tr>
            {th('n', 'name', false)}
            {th('b', 'bytes')}
            <th className="num">share</th>
            {th('o', 'objects')}
            {th('d', 'created')}
            {th('a', 'read', false)}
            <th>group</th>
            <th>top user</th>
            {markIdx && <th>state</th>}
            {showActions && <th>actions</th>}
          </tr>
        </thead>
        <tbody>
          {shown.map(k => {
            const synthetic = k.n.startsWith('(')
            const kidSegs = [...segs, k.n]
            const uri = scheme + kidSegs.join('/')
            const seg = domTeamSeg(k)
            const [u] = k.us?.[0] ?? [null]
            const mk = markIdx && !synthetic ? markIdx.resolve(uri) : null
            return (
              <tr key={k.n}>
                <td className="prefix" title={uri}>
                  {synthetic || !k.c?.length ? (
                    k.n
                  ) : (
                    <a role="link" tabIndex={0} onClick={() => onOpen(kidSegs)}>{k.n}</a>
                  )}
                </td>
                <td className="num">{fmtBytes(k.b)}</td>
                <td className="num">{node.b ? ((100 * k.b) / node.b).toFixed(1) : 0}%</td>
                <td className="num">{fmtN(k.o)}</td>
                <td>{k.d != null ? epochDaysToMonth(k.d) : '—'}</td>
                <td title={k.a != null ? 'most recent GET/HEAD/LIST under this prefix (access logs)' : undefined}>
                  {k.a != null ? epochDaysToDate(k.a) : '—'}
                </td>
                <td>{seg ? groupLabel(seg.team) : '—'}</td>
                <td>{u ?? '—'}</td>
                {markIdx && (
                  <td>
                    {mk?.mark ? (
                      <Tooltip content={markProvenance(mk.mark, mk.own)}>
                        <span className="chip" style={{ borderColor: ACTION_COLORS[mk.mark.action] }}>
                          <span className="sw" style={{ background: ACTION_COLORS[mk.mark.action] }} />
                          {ACTION_LABELS[mk.mark.action]}{mk.own ? '' : ' ⌃'}
                        </span>
                      </Tooltip>
                    ) : synthetic ? null : (
                      <span className="chip unmarked"><span className="sw" style={{ background: 'var(--mk-del)' }} />unmarked</span>
                    )}
                  </td>
                )}
                {showActions && (
                  <td className="actions">
                    {synthetic ? null : (
                      <>
                        <Tooltip content={KEEP_TIP}>
                          <button type="button" className={mk?.own && mk.mark?.action === 'keep' ? 'on' : ''} onClick={() => mark(uri, 'keep')}>keep</button>
                        </Tooltip>
                        {looksCkpt(k, uri) && (
                          <Tooltip content={KLC_TIP}>
                            <button type="button" className={mk?.own && mk.mark?.action === 'keep_last_ckpt' ? 'on' : ''} onClick={() => mark(uri, 'keep_last_ckpt')}>last ckpt</button>
                          </Tooltip>
                        )}
                        <Tooltip content={SWEEP_TIP}>
                          <button type="button" className={mk?.own && mk.mark?.action === 'sweep' ? 'on' : ''} onClick={() => mark(uri, 'sweep')}>sweep</button>
                        </Tooltip>
                        {mk?.own && (
                          <Tooltip content={clearTip(true)}>
                            <button type="button" onClick={() => mark(uri, null)}>clear</button>
                          </Tooltip>
                        )}
                        {!markIdx!.claimOf(uri) && (
                          <Tooltip content={CLAIM_TIP}>
                            <button type="button" onClick={() => claim.mutate({ prefix: uri + '/' })}>claim</button>
                          </Tooltip>
                        )}
                      </>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
      {kids.length > limit && (
        <button type="button" className="more" onClick={() => setLimit(l => l + PAGE)}>
          show {Math.min(PAGE, kids.length - limit)} more of {kids.length - limit}
        </button>
      )}
    </section>
  )
}
