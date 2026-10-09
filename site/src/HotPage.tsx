import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { HOT_DATES, HotUnavailable, changeHotDraft, exactInteger, hotDraft, hotDraftParams, hotRequest, loadHot, type HotDraft, type HotRequest, type HotResult } from './hotModel'
import { hotBucketHref, hotDrillRequest } from './hotDrillModel'
import { HotDrill } from './HotDrill'
import { SiteKbd } from './SiteKbd'
import { useDocTitle } from './title'
import { HotMaps } from './HotMaps'
import './hot.scss'

export function HotSearchFields({ draft, onChange, dates }: { draft: HotDraft; onChange: (field: keyof HotDraft, value: string) => void; dates?: readonly string[] }) {
  const { date, name, from } = draft
  const scans = dates ?? HOT_DATES
  return <>
    <label className="hot-literal">Name contains<input name="name" value={name} onChange={event => onChange('name', event.target.value)} required aria-describedby="hot-availability" /></label>
    <label>Scan<select name="date" value={date} onChange={event => onChange('date', event.target.value)}>{!scans.some(day => day === date) && <option value={date}>{date} (unavailable)</option>}{scans.map(day => <option key={day}>{day}</option>)}</select></label>
    <label>Baseline scan (optional)<select name="from" value={from} onChange={event => onChange('from', event.target.value)}><option value="">No baseline</option>{dates ? <>
      {from && (!dates.some(day => day === from) || from >= date) && <option value={from}>{from} (unavailable)</option>}
      {dates.filter(day => day < date).map(day => <option key={day}>{day}</option>)}
    </> : <>{from && from !== HOT_DATES[0] && <option value={from}>{from} (unavailable)</option>}<option value={HOT_DATES[0]} disabled={date !== HOT_DATES[1]}>{HOT_DATES[0]}</option></>}</select></label>
  </>
}

export function HotSearchForm({ params, onSearch, dates }: { params: URLSearchParams; onSearch: (next: URLSearchParams) => void; dates?: readonly string[] }) {
  const [draft, setDraft] = useState(() => hotDraft(params))
  return <form autoComplete="off" onSubmit={event => {
    event.preventDefault()
    onSearch(hotDraftParams(draft))
  }}>
    <HotSearchFields draft={draft} dates={dates} onChange={(field, value) => setDraft(current => changeHotDraft(current, field, value))} />
    <button type="submit">Search</button>
  </form>
}

export function HotTotals({ result, request }: { result: HotResult; request?: HotRequest }) {
  const { before, after, delta } = result
  const rows = [{ path: 'All buckets', after: after.root, before: before?.root, delta },
    ...after.buckets.map((row, i) => ({ path: row.path, after: row, before: before?.buckets[i],
      delta: before ? { b: row.b - before.buckets[i].b, o: row.o - before.buckets[i].o } : undefined }))]
  return <div className="hot-table-scroll" tabIndex={0} aria-label="Exact fleet and bucket totals">
    <table><caption>Matching coverage for “{after.pattern}”{before ? ` from ${before.date} to ${after.date}` : ` on ${after.date}`}</caption>
      <thead>{before ? <>
        <tr><th rowSpan={2} scope="col">Scope</th><th colSpan={2} scope="colgroup">{before.date}</th><th colSpan={2} scope="colgroup">{after.date}</th><th colSpan={2} scope="colgroup">Change</th></tr>
        <tr>{['before', 'after', 'change'].flatMap(key => [<th key={key + 'b'} scope="col">Bytes</th>, <th key={key + 'o'} scope="col">Objects</th>])}</tr>
      </> : <tr><th scope="col">Scope</th><th scope="col">Bytes</th><th scope="col">Objects</th></tr>}</thead>
      <tbody>{rows.map((row, i) => <tr key={row.path} className={i === 0 ? 'hot-fleet' : undefined}>
        <th scope="row">{i > 0 && request ? <a href={hotBucketHref(request, row.path)}>{row.path}</a> : row.path}</th>
        {before && <><td>{exactInteger(row.before!.b)}</td><td>{exactInteger(row.before!.o)}</td></>}
        <td>{exactInteger(row.after.b)}</td><td>{exactInteger(row.after.o)}</td>
        {before && <><td>{exactInteger(row.delta!.b, true)}</td><td>{exactInteger(row.delta!.o, true)}</td></>}
      </tr>)}</tbody>
    </table>
  </div>
}

export function HotPage() {
  useDocTitle('Name search preview')
  const [params, setParams] = useSearchParams()
  let request, drillRequest, issue: string | undefined
  try { if (params.has('path')) drillRequest = hotDrillRequest(params); else request = hotRequest(params) } catch (error) { issue = (error as Error).message }
  const query = useQuery({ queryKey: ['hot-l1', request?.date, request?.name, request?.from],
    queryFn: ({ signal }) => loadHot(request!, signal), enabled: !!request, staleTime: Infinity, retry: false })
  return <main className="hot-page">
    <header><Link to="/">marin GCS</Link><h1>Name search — bucket summaries</h1><p>Frozen preview: October 4–5, 2026. Fleet summaries and registered bucket detail.</p></header>
    <p className="hot-scope">Case-insensitive literal substring within any path component name; no slash-crossing. Matching directories cover their descendants, counted once. Exact bytes and object counts, including zero-byte objects.</p>
    <HotSearchForm key={params.toString()} params={params} onSearch={setParams} />
    <p id="hot-availability" className="hot-note">Only registered literals of 1–16 characters are available: at least 100,000 file or directory paths have a matching final component name on either October 4 or October 5. Other literals are unavailable, not zero matches; they do not trigger a fleet scan.</p>
    <p className="hot-note">With a baseline, the table shows all matching coverage in each snapshot and the selected scan minus the baseline. It does not restrict the results to paths that changed.</p>
    {issue && <p role="alert">{issue}</p>}
    {!issue && !drillRequest && query.isPending && <p role="status">Loading exact root totals…</p>}
    {!issue && !drillRequest && query.error && <p role={query.error instanceof HotUnavailable ? 'status' : 'alert'}>{query.error.message}</p>}
    {!issue && request && query.data && <><HotMaps result={query.data} request={request} /><HotTotals result={query.data} request={request} /></>}
    {!issue && drillRequest && <HotDrill key={params.toString()} request={drillRequest} />}
    <footer>Bucket links open prepared detail when available; no deeper drill-down, owner or Boolean filters in this preview. The main <Link to="/">storage map</Link> is unchanged.</footer>
    <SiteKbd />
  </main>
}
