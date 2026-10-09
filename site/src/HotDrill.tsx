import { DEFAULT_PALETTE } from '@rdub/treemap'
import { useQuery } from '@tanstack/react-query'
import { HotSnapshotMap } from './HotMaps'
import { HotUnavailable, exactInteger } from './hotModel'
import { hotBucketHref, loadHotDrill, type HotDrillRequest, type HotDrillResult, type HotDrillView } from './hotDrillModel'
import { type HotMapSnapshot } from './hotTreemap'

export function hotDrillMap(view: HotDrillView): HotMapSnapshot {
  const palette = ['#3c8cdd', '#e88c30', '#34b288', '#d7425b', ...DEFAULT_PALETTE.slice(4)]
  const children = view.children.map((row, i) => ({ path: row.path, b: row.b, o: row.o, color: palette[i % palette.length] }))
  children.push({ path: 'Other (folded)', ...view.other, color: '#858590' })
  return { date: view.date, pattern: view.pattern, root: { path: view.path, ...view.root, children: children.filter(row => row.b > 0) },
    empty: view.root.b ? null : view.root.o ? `${exactInteger(view.root.o)} matching objects total 0 bytes, so no byte area is drawn. Exact counts remain in the table.` : 'No matching bytes or objects in this snapshot.' }
}
export function HotDrillTotals({ result }: { result: HotDrillResult }) {
  const { before, after } = result
  const rows = [{ path: after.path, after: after.root, before: before?.root },
    ...after.children.map((child, i) => ({ path: child.path, after: child, before: before?.children[i] })),
    { path: 'Other (folded)', after: after.other, before: before?.other }]
  return <div className="hot-table-scroll" tabIndex={0} aria-label="Exact bucket, child and folded totals"><table>
    <caption>Matching coverage for “{after.pattern}” in {after.path}{before ? ` from ${before.date} to ${after.date}` : ` on ${after.date}`}</caption>
    <thead>{before ? <><tr><th rowSpan={2} scope="col">Scope</th><th colSpan={2} scope="colgroup">{before.date}</th><th colSpan={2} scope="colgroup">{after.date}</th><th colSpan={2} scope="colgroup">Change</th></tr>
      <tr>{['before', 'after', 'change'].flatMap(key => [<th key={key + 'b'} scope="col">Bytes</th>, <th key={key + 'o'} scope="col">Objects</th>])}</tr></>
      : <tr><th scope="col">Scope</th><th scope="col">Bytes</th><th scope="col">Objects</th></tr>}</thead>
    <tbody>{rows.map((row, i) => <tr key={row.path} className={i === 0 ? 'hot-fleet' : undefined}><th scope="row">{row.path}</th>
      {before && <><td>{exactInteger(row.before!.b)}</td><td>{exactInteger(row.before!.o)}</td></>}
      <td>{exactInteger(row.after.b)}</td><td>{exactInteger(row.after.o)}</td>
      {before && <><td>{exactInteger(row.after.b - row.before!.b, true)}</td><td>{exactInteger(row.after.o - row.before!.o, true)}</td></>}
    </tr>)}</tbody>
  </table></div>
}
export function HotDrill({ request }: { request: HotDrillRequest }) {
  const query = useQuery({ queryKey: ['hot-l2', request.date, request.name, request.from, request.path], queryFn: ({ signal }) => loadHotDrill(request, signal), staleTime: Infinity, retry: false })
  const result = query.data
  return <section aria-label="Bucket detail"><nav aria-label="Bucket breadcrumb"><a href={hotBucketHref(request)}>All buckets</a> / {request.path}</nav>
    <p className="hot-note">Prepared bucket detail for registered literals. Child names label summarized matching content and need not contain the search literal. Other is the exact folded remainder, including the bucket's own objects. No child drill-down.</p>
    {query.isPending && <p role="status">Loading exact bucket detail…</p>}
    {query.error && <p role={query.error instanceof HotUnavailable ? 'status' : 'alert'}>{query.error.message}</p>}
    {result && <><section className="hot-maps" aria-label="Matching bytes by child"><h2>Matching bytes by child</h2>
      <p>“{result.after.pattern}” in {request.path}{result.before ? ` from ${result.before.date} to ${result.after.date}` : ` on ${result.after.date}`}</p>
      <div className={'hot-map-pair' + (result.before ? ' comparison' : '')}>{(result.before ? [result.before, result.after] : [result.after]).map(view => <HotSnapshotMap key={`${view.date}:${view.pattern}:${view.path}`} snapshot={hotDrillMap(view)} noun="region" />)}</div>
      <p className="hot-note">Each map fills its own snapshot's matching bytes; zero-byte objects have no area. Children use one fixed partition across both dates; Other folds children below the paired cutoff ({exactInteger(result.threshold)} bytes). Exact counts and small counterparts stay in the table.</p>
    </section><HotDrillTotals result={result} /></>}
  </section>
}
