import { useEffect, useMemo, useState } from 'react'
import { intParam, useUrlState } from 'use-prms'
import { dateColor, epochDaysToMonth } from './colors'
import type { TreeNode } from './types'
import { fmtN } from './types'
import { useUnits } from './units'

// Sortable, paged listing of the treemap's current node's children — the
// tabular twin of the map above it: clicking a row drills the map into it. A
// plain directory listing (name / bytes / share / objects / created); the
// owner / fate / mark / bulk-select columns are the gcs sweep+ownership axis,
// not this deployment (see specs/branch-parity-discipline.md).

type SortKey = 'n' | 'b' | 'o' | 'd'

// Rows per page: `?n=` (default 20); the pager offers the usual sizes.
const PAGE_SIZES = [20, 50, 100, 200]

export function ChildrenTable({ node, segs, scheme = 's3://', onOpen }: {
  /** The treemap's currently-viewed node. */
  node: TreeNode
  /** Path segments from the tree root to `node` (no scheme, no root). */
  segs: string[]
  scheme?: string
  onOpen: (segs: string[]) => void
}) {
  const { fmtBytes } = useUnits()
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'b', asc: false })
  const [page, setPage] = useState(0)
  const [nP, setNP] = useUrlState('n', intParam(20))
  const PAGE = PAGE_SIZES.includes(nP) ? nP : 20

  const kids = useMemo(() => {
    const ks = (node.c ?? []).slice()
    const dir = sort.asc ? 1 : -1
    const val = (n: TreeNode): number | string =>
      sort.k === 'n' ? n.n : sort.k === 'b' ? n.b : sort.k === 'o' ? n.o : n.d ?? -Infinity
    return ks.sort((a, b) => {
      const va = val(a)
      const vb = val(b)
      return (typeof va === 'string' ? va.localeCompare(vb as string) : (va as number) - (vb as number)) * dir
    })
  }, [node, sort])
  // A new listing (drill, sort) starts on page 1.
  useEffect(() => setPage(0), [node, sort])

  // Created-month ink: an age gradient over the listed rows' range, so a
  // column of dates also reads at a glance as older ↔ newer.
  const [dMin, dMax] = useMemo(() => {
    const ds = kids.map(k => k.d).filter((d): d is number => d != null)
    return ds.length ? [Math.min(...ds), Math.max(...ds)] : [0, 0]
  }, [kids])
  const ageInk = (d: number) => dateColor(dMax > dMin ? (d - dMin) / (dMax - dMin) : 1)

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
  const pages = Math.max(1, Math.ceil(kids.length / PAGE))
  const pg = Math.min(page, pages - 1)
  const shown = kids.slice(pg * PAGE, (pg + 1) * PAGE)
  const totB = kids.reduce((s, k) => s + k.b, 0)
  const totO = kids.reduce((s, k) => s + k.o, 0)
  const pager = pages > 1 && (
    <div className="pager">
      <button type="button" disabled={pg === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
      <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous page">‹</button>
      <span>{pg * PAGE + 1}–{Math.min(kids.length, (pg + 1) * PAGE)} of {kids.length.toLocaleString('en-US')}</span>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label="next page">›</button>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
      <select className="psize" value={PAGE} onChange={e => { setNP(+e.target.value); setPage(0) }} aria-label="rows per page">
        {PAGE_SIZES.map(n => <option key={n} value={n}>{n} / page</option>)}
      </select>
    </div>
  )
  return (
    <section className="children-tbl">
      {pager}
      <table className="worklist">
        <thead>
          <tr>
            {th('n', 'name', false)}
            {th('b', 'bytes')}
            <th className="num">share</th>
            {th('o', 'objects')}
            {th('d', 'created')}
          </tr>
        </thead>
        <tbody>
          {shown.map(k => {
            const synthetic = k.n.startsWith('(')
            const kidSegs = [...segs, k.n]
            const uri = scheme + kidSegs.join('/')
            return (
              <tr key={k.n}>
                <td className="prefix" title={uri}>
                  {synthetic || !k.c?.length ? k.n : <a role="link" tabIndex={0} onClick={() => onOpen(kidSegs)}>{k.n}</a>}
                </td>
                <td className="num">{fmtBytes(k.b)}</td>
                <td className="num">{node.b ? ((100 * k.b) / node.b).toFixed(1) : 0}%</td>
                <td className="num">{fmtN(k.o)}</td>
                <td className="created num">{k.d != null
                  ? <span className="cm"><i style={{ background: ageInk(k.d) }} />{epochDaysToMonth(k.d)}</span>
                  : '—'}</td>
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          <tr className="total-row">
            <td>total{kids.length !== (node.c ?? []).length ? ` (${kids.length} shown)` : ''}</td>
            <td className="num">{fmtBytes(totB)}</td>
            <td className="num">{node.b ? ((100 * totB) / node.b).toFixed(1) : 0}%</td>
            <td className="num">{fmtN(totO)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
      {pager}
    </section>
  )
}
