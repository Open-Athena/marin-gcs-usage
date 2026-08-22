import { useMemo, useState } from 'react'
import { epochDaysToDate, epochDaysToMonth } from './colors'
import { ACTION_COLORS } from './MarkControls'
import type { MarkIndex } from './marks'
import { ACTION_LABELS } from './marks'
import type { TreeNode } from './types'
import { domTeamSeg, fmtN, groupLabel } from './types'
import { useUnits } from './units'

// Sortable/paginated listing of the treemap's current node's children — the
// tabular twin of the map above it (same drill: clicking a row opens it).

type SortKey = 'n' | 'b' | 'o' | 'd' | 'a'

const PAGE = 50

export function ChildrenTable({ node, segs, scheme, markIdx, onOpen }: {
  /** The treemap's currently-viewed node. */
  node: TreeNode
  /** Path segments from the tree root to `node` (no scheme, no root). */
  segs: string[]
  scheme: string
  markIdx?: MarkIndex | null
  onOpen: (segs: string[]) => void
}) {
  const { fmtBytes } = useUnits()
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'b', asc: false })
  const [limit, setLimit] = useState(PAGE)

  const kids = useMemo(() => {
    const ks = (node.c ?? []).slice()
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
  }, [node, sort])

  if (!kids.length) return null
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
                      <span className="chip" title={`${mk.mark.who}${mk.own ? '' : ` (inherited from ${mk.mark.prefix})`}`}
                        style={{ borderColor: ACTION_COLORS[mk.mark.action] }}>
                        <span className="sw" style={{ background: ACTION_COLORS[mk.mark.action] }} />
                        {ACTION_LABELS[mk.mark.action]}{mk.own ? '' : ' ⌃'}
                      </span>
                    ) : synthetic ? null : (
                      <span className="chip unmarked"><span className="sw" style={{ background: 'var(--mk-del)' }} />unmarked</span>
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
