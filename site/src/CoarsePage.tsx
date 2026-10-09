import { useQuery } from '@tanstack/react-query'
import { Treemap, slotColor } from '@rdub/treemap'
import { Link, useSearchParams } from 'react-router-dom'
import { coarseError, parseCoarse, parseCoarseDiff, type CoarseBody, type CoarseCell } from './coarseModel'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'
import './coarse.scss'

export function CoarsePage() {
  const [params, setParams] = useSearchParams()
  const { fmtBytes } = useUnits()
  const date = params.get('date') ?? '2026-10-05'
  const date0 = params.get('date0') ?? ''
  const name = params.get('name') ?? 'zarr.json'
  const mode = params.get('mode') ?? 'exact'
  const path = params.get('path') ?? ''
  const budget = params.get('budget') ?? '64'
  const levels = mode === 'coverage' ? '1' : params.get('levels') ?? '1'
  const query = new URLSearchParams({ date, name, mode, path, budget, levels })
  if (date0) query.set('date0', date0)
  const result = useQuery({
    queryKey: ['coarse', date, date0, name, mode, path, budget, levels],
    queryFn: async ({ signal }) => {
      const res = await fetch(`/api/coarse?${query}`, { signal })
      if (!res.ok) throw coarseError(res.status, await res.text())
      const data: unknown = await res.json()
      return date0 ? { diff: parseCoarseDiff(data) } : { single: parseCoarse(data) }
    },
    staleTime: Infinity,
    retry: false,
  })
  const href = (p: string) => {
    const next = new URLSearchParams(query)
    next.set('path', p)
    return `/coarse?${next}`
  }
  const drill = (p: string) => { const next = new URLSearchParams(query); next.set('path', p); setParams(next) }
  const segments = path.split('/').filter(Boolean)
  const comparison = result.data?.diff
  const body = comparison?.after ?? result.data?.single
  const colors = new Map(body?.tree.children?.filter(c => !c.folded).map((c, i) => [c.path, slotColor(i)]))
  const signedBytes = (n: number) => `${n > 0 ? '+' : n < 0 ? '−' : ''}${fmtBytes(Math.abs(n))}`
  const signedCount = (n: number) => `${n > 0 ? '+' : ''}${n.toLocaleString()}`
  const counts = (cell: CoarseCell) => `${cell.matches === null ? '' : `${cell.matches.toLocaleString()} matching paths; `}${cell.o.toLocaleString()} objects`
  const detail = (cell: CoarseCell) => <div><strong>{cell.folded ? '(other)' : cell.path || 'all buckets'}</strong><p>{fmtBytes(cell.b)}; {counts(cell)}</p>
    <p>{cell.folded ? 'Exact remainder outside the displayed child partition.' : cell.leaf ? 'Matching leaf path.' : 'Click to show the matching bytes inside this directory.'}</p></div>
  const map = (view: CoarseBody, index = 0) => <section key={`${index}:${view.date}`}>
    {comparison && <h2>{view.date} · {fmtBytes(view.tree.b)} · {counts(view.tree)}</h2>}
    {view.tree.b > 0 && !view.tree.leaf ? <div className="coarse-map"><Treemap key={`${view.date}:${name}:${mode}:${path}:${budget}:${levels}`} root={view.tree}
      getSize={n => n.b} getChildren={n => n.children} getLabel={n => n.label} getId={n => `${n.folded ? 'other:' : 'path:'}${n.path}`}
      formatSize={fmtBytes} chrome={false} minCellArea={null} minCellSide={null}
      colorForCell={(n, branch, _depth, ctx) => n.folded ? { bg: 'var(--other)', ink: 'var(--cell-ink)' } : ctx.hasKids ?
        { bg: 'var(--dt-treemap-container-bg)', ink: 'var(--dt-treemap-ink)' } :
        { bg: colors.get(branch[1]?.path ?? n.path) ?? slotColor(0), ink: '#fff' }}
      cellHref={n => n.folded || n.leaf ? undefined : href(n.path)}
      onCellClick={n => { if (n.folded || n.leaf) return false; drill(n.path); return true }}
      renderTooltip={n => detail(n)} renderTip={(label, button) => <Tooltip content={label}>{button}</Tooltip>}
    /></div> : <p>{view.tree.leaf ? `${fmtBytes(view.tree.b)} at this leaf.` : 'No matching bytes at this path.'} {counts(view.tree)} retained in the exact totals.</p>}
  </section>
  const diffRows = comparison ? (() => {
    const before = comparison.before.tree.children?.filter(c => !c.folded) ?? []
    const after = new Map(comparison.after.tree.children?.filter(c => !c.folded).map(c => [c.path, c]))
    const rows = before.map(c => ({ path: c.path, label: c.label, before: c, after: after.get(c.path)! }))
    if (Object.values(comparison.before.other).some(Boolean) || Object.values(comparison.after.other).some(Boolean)) {
      rows.push({ path: '', label: '(other)', before: { path: '', label: '(other)', leaf: true, folded: true, ...comparison.before.other },
        after: { path: '', label: '(other)', leaf: true, folded: true, ...comparison.after.other } })
    }
    return rows
  })() : []
  return <main className="coarse-page">
    <header><Link to="/">marin GCS</Link><h1>Coarse path search</h1><span>dev prototype</span></header>
    <p className="coarse-scope">{mode === 'coverage' ? 'Case-insensitive full-path substring. Matching directories cover their descendants, with overlapping matches counted once. Exact bytes/object totals; matching-path counts are not computed. One directory level per drill.' : 'Case-insensitive literal match against leaf basenames. Exact bytes and counts with bounded directory refinement. Patterns matching directory names are refused in these leaf modes.'} No owner, age, class or Boolean filters.</p>
    <form key={`${date}:${date0}:${name}:${mode}:${budget}:${levels}`} onSubmit={event => {
      event.preventDefault()
      const fields = new FormData(event.currentTarget)
      const before = String(fields.get('date0'))
      const nextMode = String(fields.get('mode'))
      const next = new URLSearchParams({ date: String(fields.get('date')), name: String(fields.get('name')), mode: nextMode, budget: before && Number(fields.get('budget')) > 128 ? '64' : String(fields.get('budget')), levels: nextMode === 'coverage' ? '1' : String(fields.get('levels') ?? '1'), path })
      if (before) next.set('date0', before)
      setParams(next)
    }}>
      <label>Literal pattern<input name="name" defaultValue={name} required maxLength={512} /></label>
      <label>Match<select name="mode" defaultValue={mode}><option value="exact">Exact basename</option><option value="contains">Contains (leaf only)</option><option value="suffix">Ends with</option><option value="coverage">Full path (bytes/objects)</option></select></label>
      <label>Scan<select name="date" defaultValue={date}><option>2026-10-04</option><option>2026-10-05</option></select></label>
      <label>Compare from<select name="date0" defaultValue={date0}><option value="">No comparison</option><option>2026-10-04</option><option>2026-10-05</option></select></label>
      <label>Child budget<select name="budget" defaultValue={budget}><option>16</option><option>64</option>{date0 ? <option>128</option> : <option>256</option>}</select></label>
      <label>Directory levels<select name="levels" defaultValue={levels} disabled={mode === 'coverage'}><option>1</option><option>2</option><option>3</option><option>4</option></select></label>
      <button type="submit">Search</button>
    </form>
    <nav aria-label="Drilled path"><Link to={href('')}>all buckets</Link>{segments.map((segment, i) => <span key={i}> / <Link to={href(segments.slice(0, i + 1).join('/'))}>{segment}</Link></span>)}</nav>
    {result.isPending && <p role="status">Loading exact directory totals…</p>}
    {result.error && <p role="alert">{result.error.message} Try exact zarr.json, .npy in a leaf mode, or datakit in Full path mode. Only the frozen scans are supported.</p>}
    {body && <>
      <p className="coarse-total">{comparison ? `${signedBytes(comparison.delta.b)}; ${comparison.delta.matches === null ? '' : `${signedCount(comparison.delta.matches)} matching paths; `}${signedCount(comparison.delta.o)} objects.` : `${fmtBytes(body.tree.b)}; ${counts(body.tree)}.`} Server: {(comparison?.seconds ?? body.seconds).toFixed(3)}s ({(comparison?.cacheHit ?? body.cacheHit) ? 'resident summary' : 'summary built for this request'}).</p>
      <div className={comparison ? 'coarse-comparison' : undefined}>{comparison ? [comparison.before, comparison.after].map(map) : map(body)}</div>
      <p>Children below {fmtBytes(body.threshold)}{comparison ? ' on both dates' : ''} are folded into an exact remainder at every expanded directory. Showing up to {body.levels} directory level{body.levels === 1 ? '' : 's'} with one fixed byte threshold. Click a directory to refine.{comparison && ' Both maps share each partition; up to twice the selected budget can be shown per level.'} {body.mode === 'coverage' ? 'Zero-byte objects contribute to object counts but have no area.' : 'Zero-byte matches contribute to counts but have no area.'}</p>
      {comparison ? <table><thead><tr><th>Directory or file</th><th>{comparison.before.date}</th><th>{comparison.after.date}</th><th>Byte change</th><th>{body.mode === 'coverage' ? 'Object change' : 'Path change'}</th></tr></thead>
        <tbody>{diffRows.map(row => <tr key={`${row.before.folded}:${row.path}`}><td>{row.before.folded ? '(other)' : <Link to={href(row.path)}>{row.label}</Link>}</td>
          <td>{fmtBytes(row.before.b)}</td><td>{fmtBytes(row.after.b)}</td><td>{signedBytes(row.after.b - row.before.b)}</td><td>{signedCount(body.mode === 'coverage' ? row.after.o - row.before.o : row.after.matches! - row.before.matches!)}</td></tr>)}</tbody>
      </table> : <table><thead><tr><th>Directory or file</th><th>Matched bytes</th>{body.mode !== 'coverage' && <th>Matching paths</th>}<th>Objects</th></tr></thead>
        <tbody>{body.tree.children?.map(child => <tr key={`${child.folded}:${child.path}`}><td>{child.folded ? '(other)' : <Link to={href(child.path)}>{child.label}</Link>}</td><td>{fmtBytes(child.b)}</td>{child.matches !== null && <td>{child.matches.toLocaleString()}</td>}<td>{child.o.toLocaleString()}</td></tr>)}</tbody>
      </table>}
      {body.other.b === 0 && body.other.matches !== null && body.other.matches > 0 && <p>{body.other.matches.toLocaleString()} zero-byte matching paths are folded below the display threshold.</p>}
      {body.other.b === 0 && body.other.matches === null && body.other.o > 0 && <p>{body.other.o.toLocaleString()} zero-byte objects are folded below the display threshold.</p>}
    </>}
    <footer>Frozen 10-04/10-05 indexes only. This does not replace <Link to="/">full path search</Link>.</footer>
  </main>
}
