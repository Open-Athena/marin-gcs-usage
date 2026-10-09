import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import type { TreemapProps } from '@rdub/treemap'
import { HotDrill, HotDrillTotals, hotDrillMap } from './HotDrill'
import { HotTotals } from './HotPage'
import { HotUnavailable } from './hotModel'
import type { HotDrillResult } from './hotDrillModel'
import type { HotMapNode } from './hotTreemap'

const { maps } = vi.hoisted(() => ({ maps: [] as TreemapProps<HotMapNode>[] }))
vi.mock('./SiteKbd', () => ({ SiteKbd: () => null }))
vi.mock('@rdub/treemap', async original => ({ ...await original<typeof import('@rdub/treemap')>(), Treemap: (props: TreemapProps<HotMapNode>) => { maps.push(props); return createElement('div') } }))
const request = { date: '2026-10-05', from: '2026-10-04', name: '.json', path: 'bucket-a' }
const result: HotDrillResult = { threshold: 5, budget: 64,
  before: { date: request.from, path: request.path, pattern: request.name, root: { b: 14, o: 7 }, children: [
    { path: 'bucket-a/folder', name: 'folder', pre: 2, post: 4, b: 12, o: 2 }, { path: 'bucket-a/zero', name: 'zero', pre: 5, post: 6, b: 0, o: 3 }], other: { b: 2, o: 2 } },
  after: { date: request.date, path: request.path, pattern: request.name, root: { b: 8, o: 5 }, children: [
    { path: 'bucket-a/folder', name: 'folder', pre: 2, post: 4, b: 1, o: 2 }, { path: 'bucket-a/zero', name: 'zero', pre: 5, post: 6, b: 5, o: 1 }], other: { b: 2, o: 2 } } }
function render(client: QueryClient) { return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(HotDrill, { request }))) }
const paragraphs = (html: string) => [...html.matchAll(/<p role="(status|alert)">(.*?)<\/p>/g)].map(([, role, text]) => ({ role, text }))
beforeEach(() => { maps.length = 0 })
it('shares the existing renderer with independent byte areas, stable child colors and unclickable folded Other', () => {
  const client = new QueryClient(); client.setQueryData(['hot-l2', request.date, request.name, request.from, request.path], result)
  try {
    const html = render(client)
    expect([...html.matchAll(/<p class="hot-note">(.*?)<\/p>/g)].map(([, text]) => text.replace(/&#x27;/g, "'"))).toEqual([
      "Prepared bucket detail for registered literals. Child names label summarized matching content and need not contain the search literal. Other is the exact folded remainder, including the bucket's own objects. No child drill-down.",
      "Each map fills its own snapshot's matching bytes; zero-byte objects have no area. Children use one fixed partition across both dates; Other folds children below the paired cutoff (5 bytes). Exact counts and small counterparts stay in the table.",
    ])
    expect([...html.matchAll(/<figcaption>(.*?)<\/figcaption>/g)].map(([, text]) => text)).toEqual(['2026-10-04<span>14 B matching bytes</span>', '2026-10-05<span>8 B matching bytes</span>'])
    expect([...html.matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map(([, href, label]) => [href, label])).toEqual([['/hot?name=.json&amp;date=2026-10-05&amp;from=2026-10-04', 'All buckets']])
    expect(maps.map(p => ({ area: p.getSize(p.root), children: p.root.children, links: p.cellHref, drill: p.loadChildren, tooltip: p.renderTooltip!(p.root, [p.root]) }))).toEqual([
      { area: 14, children: [{ path: 'bucket-a/folder', b: 12, o: 2, color: '#3c8cdd' }, { path: 'Other (folded)', b: 2, o: 2, color: '#858590' }], links: undefined, drill: undefined, tooltip: null },
      { area: 8, children: [{ path: 'bucket-a/folder', b: 1, o: 2, color: '#3c8cdd' }, { path: 'bucket-a/zero', b: 5, o: 1, color: '#e88c30' }, { path: 'Other (folded)', b: 2, o: 2, color: '#858590' }], links: undefined, drill: undefined, tooltip: null },
    ])
  } finally { client.clear() }
})
it('retains every exact small/zero counterpart and folded totals in the authoritative table', () => {
  const html = renderToStaticMarkup(createElement(HotDrillTotals, { result }))
  expect([...html.matchAll(/<tr(?: class="hot-fleet")?><th scope="row">(.*?)<\/th>((?:<td>.*?<\/td>)+)<\/tr>/g)].map(([, path, cells]) => [path, ...[...cells.matchAll(/<td>(.*?)<\/td>/g)].map(([, text]) => text)])).toEqual([
    ['bucket-a', '14', '7', '8', '5', '-6', '-2'], ['bucket-a/folder', '12', '2', '1', '2', '-11', '0'],
    ['bucket-a/zero', '0', '3', '5', '1', '+5', '-2'], ['Other (folded)', '2', '2', '2', '2', '0', '0'],
  ])
})
it.each([5, 0])('draws no fabricated area for %s zero-byte objects', objects => {
  const view = { ...result.after, root: { b: 0, o: objects }, children: [], other: { b: 0, o: objects } }
  expect(hotDrillMap(view)).toEqual({ date: view.date, pattern: view.pattern, root: { path: view.path, b: 0, o: objects, children: [] },
    empty: objects ? '5 matching objects total 0 bytes, so no byte area is drawn. Exact counts remain in the table.' : 'No matching bytes or objects in this snapshot.' })
})
it('unavailable detail shows the explicit nonzero-status explanation and no old table/maps', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retryOnMount: false } } })
  try {
    client.getQueryCache().build(client, { queryKey: ['hot-l2', request.date, request.name, request.from, request.path], retry: false },
      { data: undefined, dataUpdatedAt: 0, dataUpdateCount: 0, error: new HotUnavailable('Bucket detail is unavailable, not zero.'), errorUpdatedAt: 1, errorUpdateCount: 1, fetchFailureCount: 1, fetchFailureReason: null, fetchMeta: null, isInvalidated: false, status: 'error', fetchStatus: 'idle' })
    const html = render(client)
    expect(paragraphs(html)).toEqual([{ role: 'status', text: 'Bucket detail is unavailable, not zero.' }])
    expect([...html.matchAll(/<table\b/g)]).toEqual([]); expect(maps).toEqual([])
  } finally { client.clear() }
})
it('root bucket links preserve dates/literal without granting deeper drill', () => {
  const html = renderToStaticMarkup(createElement(HotTotals, { request, result: { after: {
    target: 'fixture', date: request.date, pattern: request.name, root: { b: 8, o: 5 }, buckets: [{ path: 'bucket-a', pre: 1, post: 10, b: 8, o: 5 }],
  } } }))
  expect([...html.matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map(([, href, text]) => [href, text])).toEqual([['/hot?name=.json&amp;date=2026-10-05&amp;from=2026-10-04&amp;path=bucket-a', 'bucket-a']])
})
