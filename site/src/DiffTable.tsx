import { useEffect, useMemo, useState } from 'react'
import { intParam, useUrlState } from 'use-prms'
import type { DiffData, DiffRow } from './DiffTreemap'
import { fmtN } from './types'
import { useUnits } from './units'

// Tabular twin of the diff treemap: the changed prefixes it tiles, as a
// sortable/paged list. The map shows the frontier (non-expanded, non-pruned
// rows); this lists that same frontier, sorted by |Δ| — the movement a diff is
// about. (Cross-deployment ask: the diff map wanted a row-for-row table.)

type SortKey = 'p' | 'a' | 'b' | 'delta' | 'od'

const PAGE_SIZES = [20, 50, 100, 200]

export function DiffTable({ data }: { data: DiffData }) {
  const { fmtBytes } = useUnits()
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'delta', asc: false })
  const [page, setPage] = useState(0)
  const [nP, setNP] = useUrlState('dn', intParam(20))
  const PAGE = PAGE_SIZES.includes(nP) ? nP : 20

  const rows = useMemo(() => {
    // Frontier = the map's tiles: non-expanded, non-pruned, actually-changed.
    const fr = data.rows.filter(r => !r.x && !r.pr && r.s !== 'unchanged')
    const dir = sort.asc ? 1 : -1
    const cmp = (x: DiffRow, y: DiffRow): number => {
      switch (sort.k) {
        case 'p': return x.p.localeCompare(y.p) * dir
        case 'a': return (x.a - y.a) * dir
        case 'b': return (x.b - y.b) * dir
        case 'od': return ((x.ob - x.oa) - (y.ob - y.oa)) * dir
        default: return (Math.abs(x.b - x.a) - Math.abs(y.b - y.a)) * dir // |Δ|
      }
    }
    return fr.sort(cmp)
  }, [data, sort])
  useEffect(() => setPage(0), [data, sort])

  if (!rows.length) return null
  const th = (k: SortKey, label: string, num = true) => (
    <th
      className={(num ? 'num ' : '') + 'sortable' + (sort.k === k ? ' on' : '')}
      onClick={() => setSort(s => ({ k, asc: s.k === k ? !s.asc : k === 'p' }))}
      title="sort"
    >
      {label}{sort.k === k ? (sort.asc ? ' ▲' : ' ▼') : ''}
    </th>
  )
  const pages = Math.max(1, Math.ceil(rows.length / PAGE))
  const pg = Math.min(page, pages - 1)
  const shown = rows.slice(pg * PAGE, (pg + 1) * PAGE)
  const delta = (r: DiffRow) => r.b - r.a
  const dCell = (d: number) => (
    <span className={d >= 0 ? 'grew' : 'shrank'}>{(d >= 0 ? '+' : '−') + fmtBytes(Math.abs(d))}</span>
  )
  const pager = pages > 1 && (
    <div className="pager">
      <button type="button" disabled={pg === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
      <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous page">‹</button>
      <span>{pg * PAGE + 1}–{Math.min(rows.length, (pg + 1) * PAGE)} of {rows.length.toLocaleString('en-US')}</span>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label="next page">›</button>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
      <select className="psize" value={PAGE} onChange={e => { setNP(+e.target.value); setPage(0) }} aria-label="rows per page">
        {PAGE_SIZES.map(n => <option key={n} value={n}>{n} / page</option>)}
      </select>
    </div>
  )
  return (
    <section className="children-tbl diff-tbl">
      {pager}
      <table className="worklist">
        <thead>
          <tr>
            {th('p', 'path', false)}
            {th('a', 'before')}
            {th('b', 'after')}
            {th('delta', 'Δ')}
            {th('od', 'Δ objects')}
          </tr>
        </thead>
        <tbody>
          {shown.map(r => (
            <tr key={r.p} className={`st-${r.s}`}>
              <td className="prefix" title={r.p}>{r.p}</td>
              <td className="num">{r.a ? fmtBytes(r.a) : '—'}</td>
              <td className="num">{r.b ? fmtBytes(r.b) : '—'}</td>
              <td className="num">{dCell(delta(r))}</td>
              <td className="num">{r.ob - r.oa ? ((r.ob - r.oa) > 0 ? '+' : '−') + fmtN(Math.abs(r.ob - r.oa)) : '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {pager}
    </section>
  )
}
