import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { HotMaps } from './HotMaps'
import { HotSearchForm, HotTotals } from './HotPage'
import { SiteKbd } from './SiteKbd'
import type { HotRequest } from './hotModel'
import { loadName, loadNameRegistry, nameHasDetail, namePageParams, nameRequest, nameResultForRegistry, type NameResult } from './nameModel'
import { useDocTitle } from './title'
import './hot.scss'

export function NamePlanStatus({ result }: { result: NameResult }) {
  const sides = result.before ? [{ view: result.before, execution: result.execution.before! }, { view: result.after, execution: result.execution.after }]
    : [{ view: result.after, execution: result.execution.after }]
  return <div aria-label="Name-summary execution plan">{sides.map(({ view, execution }) => <p className="hot-note" key={view.date}>
    {view.date}: {execution.plan === 'catalog' ? 'Catalog (precomputed)' : 'Bounded name postings (on demand)'}. {execution.source} — {execution.validation.description}
  </p>)}</div>
}
export function NamePage() {
  useDocTitle('Name summaries preview')
  const [rawParams, setParams] = useSearchParams(), params = namePageParams(rawParams)
  const registry = useQuery({ queryKey: ['name-summary-registry'], queryFn: ({ signal }) => loadNameRegistry(signal), staleTime: Infinity, retry: false })
  const dates = registry.data?.dates.map(row => row.date)
  let request: HotRequest | undefined, issue: string | undefined
  if (dates && !registry.error) try { request = nameRequest(rawParams, dates) } catch (error) { issue = (error as Error).message }
  const query = useQuery({ queryKey: ['name-summary', request?.date, request?.name, request?.from], queryFn: async ({ signal }) => {
    return nameResultForRegistry(await loadName(request!, signal, dates), registry.data!)
  }, enabled: !!request, staleTime: Infinity, retry: false })
  let result: NameResult | undefined
  if (request && query.data) try { result = nameResultForRegistry(query.data, registry.data!) } catch (error) { issue = (error as Error).message }
  const detail = result && nameHasDetail(result) ? request : undefined
  const catalogOnly = registry.data?.dates.filter(row => [request?.date, request?.from].includes(row.date) && row.kind === 'daily-scalar-source-v1' && !row.plans.includes('bounded-name-postings')) ?? []
  return <main className="hot-page">
    <header><Link to="/">marin GCS</Link><h1>Name search — exact root summaries</h1>{dates && !registry.error && <p>Available scans: {dates.join(', ')}.</p>}</header>
    <p className="hot-scope">Case-insensitive literal substring within any path component name; no slash-crossing. Matching directories cover their descendants, counted once. Exact bytes and object counts, including zero-byte objects.</p>
    {dates && !registry.error && <HotSearchForm key={rawParams.toString()} params={params} dates={registry.data?.dated ? dates : undefined} onSearch={setParams} />}
    {registry.data && !registry.error && <p id="hot-availability" className="hot-note">{catalogOnly.length
      ? `${catalogOnly.map(row => `${row.date}: catalog literals only, using membership qualified on ${row.qualification_dates!.join(', ')}`).join('. ')}—not a current-scan frequency claim. Other literals are unavailable for those scans, not zero matches; no on-demand fallback.`
      : 'Catalog literals use prepared summaries. Other literals use bounded name postings on demand; requests exceeding the work budget fail explicitly, not as zero matches. No unbounded fleet scan.'}</p>}
    <p className="hot-note">With a baseline, coverage is computed for each snapshot; change is the selected scan minus the baseline, not only paths that changed.</p>
    {issue && <p role="alert">{issue}</p>}
    {registry.isPending && <p role="status">Loading available name-summary scans…</p>}
    {registry.error && <p role="alert">{registry.error.message}</p>}
    {request && !issue && query.isPending && <p role="status">Loading exact name summary…</p>}
    {request && !issue && query.error && <p role="alert">{query.error.message}</p>}
    {request && !issue && result && <><NamePlanStatus result={result} /><HotMaps result={result} request={detail} /><HotTotals result={result} request={detail} />
      <p className="hot-note">{detail ? 'Prepared bucket detail is available through the map actions and table links.' : result.capabilities ? 'These dated root summaries have exact bucket totals only; no prepared bucket detail or deeper drill-down.' : 'This on-demand result has root and bucket totals only; no prepared bucket detail or deeper drill-down.'}</p></>}
    <footer>No owner or Boolean filters in this preview. The <Link to="/hot">catalog-only preview</Link> and main <Link to="/">storage map</Link> are unchanged.</footer>
    <SiteKbd />
  </main>
}
