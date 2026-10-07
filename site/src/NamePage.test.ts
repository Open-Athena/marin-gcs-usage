import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { NamePage } from './NamePage'
import { nameDiff, nameFixture } from './nameTestFixtures'
import { parseName, parseNameRegistry } from './nameModel'
import { dailyNameFixture, datedNameRegistry, legacyNameRegistry, mixedDatedNameDiff } from './datedNameTestFixtures'

vi.mock('./SiteKbd', () => ({ SiteKbd: () => null }))
const { roots } = vi.hoisted(() => ({ roots: [] as { b: number }[] }))
vi.mock('@rdub/treemap', async original => ({ ...await original<typeof import('@rdub/treemap')>(), Treemap: ({ root }: { root: { b: number } }) => { roots.push(root); return createElement('div') } }))
function render(client: QueryClient, path = '/names?date=2026-10-05&name=datakit', seedRegistry = true) {
  if (seedRegistry && !client.getQueryState(['name-summary-registry'])) client.setQueryData(['name-summary-registry'], parseNameRegistry(legacyNameRegistry()))
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(MemoryRouter, { initialEntries: [path] }, createElement(NamePage))))
}
const links = (html: string) => [...html.matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map(([, href, label]) => [href, label])
const planParagraphs = (html: string) => /<div aria-label="Name-summary execution plan">(.*?)<\/div>/.exec(html)?.[1]
const tableRows = (html: string) => [...(html.match(/<tbody>(.*?)<\/tbody>/)?.[1] ?? '').matchAll(/<tr[^>]*>(.*?)<\/tr>/g)]
  .map(([, row]) => [...row.matchAll(/<t[hd][^>]*>(.*?)<\/t[hd]>/g)].map(([, text]) => text.replace(/<[^>]+>/g, '')))
beforeEach(() => { roots.length = 0 })
it('defaults the existing controlled form to datakit and no baseline', () => {
  const client = new QueryClient()
  try {
    const html = render(client, '/names')
    expect(html.match(/<input[^>]+>/)?.[0]).toBe('<input required="" aria-describedby="hot-availability" name="name" value="datakit"/>')
    expect(html.match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="" selected="">No baseline</option><option value="2026-10-04">2026-10-04</option>')
    expect([...html.matchAll(/<p role="status">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['Loading exact name summary…'])
  } finally { client.clear() }
})
it('waits for availability without inventing scan controls or fetching a summary', () => {
  const client = new QueryClient()
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit', false)
    expect([...html.matchAll(/<p role="status">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['Loading available name-summary scans…'])
    expect([...html.matchAll(/<select\b/g)]).toEqual([])
    expect([...html.matchAll(/<table\b/g)]).toEqual([])
    expect(roots).toEqual([])
    expect(client.getQueryState(['name-summary', undefined, undefined, undefined])?.fetchStatus).toBe('idle')
  } finally { client.clear() }
})
it('offers only metadata dates and retains the mixed-date URL baseline without fake detail links', () => {
  const client = new QueryClient(), body = mixedDatedNameDiff()
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  client.setQueryData(['name-summary', body.date, body.pattern, body.from], parseName(body, { date: body.date, name: body.pattern, from: body.from }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit&from=2026-10-05')
    expect(html.match(/<select name="date">(.*?)<\/select>/)?.[1]).toBe('<option>2026-10-04</option><option>2026-10-05</option><option selected="">2026-10-06</option>')
    expect(html.match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="">No baseline</option><option>2026-10-04</option><option selected="">2026-10-05</option>')
    expect(html.match(/<p id="hot-availability" class="hot-note">(.*?)<\/p>/)?.[1]).toBe('2026-10-06: catalog literals only, using membership qualified on 2026-10-04, 2026-10-05—not a current-scan frequency claim. Other literals are unavailable for those scans, not zero matches; no on-demand fallback.')
    expect(links(html)).toEqual([['/', 'marin GCS'], ['/hot', 'catalog-only preview'], ['/', 'storage map']])
    expect(roots.map(row => row.b)).toEqual([8, 10])
    expect(tableRows(html)).toEqual([
      ['All buckets', '8', '5', '10', '6', '+2', '+1'], ['bucket-a', '8', '3', '10', '4', '+2', '+1'], ['bucket-b', '0', '2', '0', '2', '0', '0'],
      ['bucket-c', '0', '0', '0', '0', '0', '0'], ['bucket-d', '0', '0', '0', '0', '0', '0'], ['bucket-e', '0', '0', '0', '0', '0', '0'], ['bucket-f', '0', '0', '0', '0', '0', '0'],
    ])
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([])
  } finally { client.clear() }
})
it('single dated scan reload has no baseline and preserves zero-byte object counts in the exact table', () => {
  const client = new QueryClient(), body = dailyNameFixture(); body.root = { b: 0, o: 2 }; body.buckets.forEach(row => { row.b = 0; row.o = row.path === 'bucket-b' ? 2 : 0 })
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit')
    expect(html.match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="" selected="">No baseline</option><option>2026-10-04</option><option>2026-10-05</option>')
    expect(tableRows(html)).toEqual([['All buckets', '0', '2'], ['bucket-a', '0', '0'], ['bucket-b', '0', '2'], ['bucket-c', '0', '0'], ['bucket-d', '0', '0'], ['bucket-e', '0', '0'], ['bucket-f', '0', '0']])
    expect(links(html)).toEqual([['/', 'marin GCS'], ['/hot', 'catalog-only preview'], ['/', 'storage map']])
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([])
  } finally { client.clear() }
})
it.each(['gcs_other', 'gcs'])('refuses a cached response with unrelated store/source (%s) before display', store => {
  const client = new QueryClient(), body = dailyNameFixture(); body.logical_store = store
  if (store === 'gcs') body.source_identity.artifact_sha256 = 'e'.repeat(64)
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit')
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([store === 'gcs_other'
      ? 'Name summary returned a different logical store or bucket set from the available-scan registry.' : 'Name summary returned a different pinned daily source from the available-scan registry.'])
    expect(tableRows(html)).toEqual([])
    expect(roots).toEqual([])
  } finally { client.clear() }
})
it('unknown dates remain explicit unavailable selections rather than silently changing the URL', () => {
  const client = new QueryClient()
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  try {
    const html = render(client, '/names?date=2026-10-07&name=datakit')
    expect(html.match(/<select name="date">(.*?)<\/select>/)?.[1]).toBe('<option value="2026-10-07" selected="">2026-10-07 (unavailable)</option><option>2026-10-04</option><option>2026-10-05</option><option>2026-10-06</option>')
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['This scan is unavailable in the name-summary registry; it is not a zero-match result.'])
    expect(roots).toEqual([])
  } finally { client.clear() }
})
it('failed metadata shows no guessed dates, cached result, maps or totals', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retryOnMount: false } } }), message = 'Available name-summary scans could not be loaded. Try again; no dates or zero results have been inferred.'
  client.getQueryCache().build(client, { queryKey: ['name-summary-registry'] }, { data: undefined, dataUpdatedAt: 0, dataUpdateCount: 0,
    error: new Error(message), errorUpdatedAt: 1, errorUpdateCount: 1, fetchFailureCount: 1, fetchFailureReason: null, fetchMeta: null, isInvalidated: false, status: 'error', fetchStatus: 'idle' })
  client.setQueryData(['name-summary', '2026-10-05', 'datakit', undefined], parseName(nameFixture(), { date: '2026-10-05', name: 'datakit' }))
  try {
    const html = render(client, '/names', false)
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([message])
    expect([...html.matchAll(/<select\b|<table\b/g)]).toEqual([])
    expect(roots).toEqual([])
  } finally { client.clear() }
})
it.each(['catalog', 'bounded-name-postings'] as const)('renders truthful %s status and only catalog detail links', plan => {
  const client = new QueryClient(), body = nameFixture(plan)
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client)
    expect(planParagraphs(html)).toBe(`<p class="hot-note">2026-10-05: ${plan === 'catalog' ? 'Catalog (precomputed)' : 'Bounded name postings (on demand)'}. ${body.source} — ${body.validation.description}</p>`)
    expect(links(html)).toEqual([['/', 'marin GCS'], ...(plan === 'catalog' ? Array.from('abcdef', letter => [`/hot?name=datakit&amp;date=2026-10-05&amp;path=bucket-${letter}`, `bucket-${letter}`]) : []), ['/hot', 'catalog-only preview'], ['/', 'storage map']])
    expect([...html.matchAll(/<span>Hover a bucket(.*?)<\/span>/g)].map(([, text]) => text)).toEqual([plan === 'catalog'
      ? ' or press Enter on it for exact totals. Select a bucket to open prepared detail, or use the table links.'
      : ' or press Enter on it for exact totals. No deeper drill-down.'])
    expect(roots.map(row => row.b)).toEqual([8])
  } finally { client.clear() }
})
it('mixed comparison shows both source plans and no prepared bucket detail', () => {
  const client = new QueryClient(), body = nameDiff('catalog', 'bounded-name-postings')
  client.setQueryData(['name-summary', '2026-10-05', 'datakit', '2026-10-04'], parseName(body, { date: '2026-10-05', name: 'datakit', from: '2026-10-04' }))
  try {
    const html = render(client, '/names?date=2026-10-05&name=datakit&from=2026-10-04')
    expect(planParagraphs(html)).toBe('<p class="hot-note">2026-10-04: Catalog (precomputed). registered precomputed batch artifact — pinned catalog validation</p><p class="hot-note">2026-10-05: Bounded name postings (on demand). bounded dated name postings; directory rollups are atomic — bounded exact first-hit coverage; no per-request source oracle</p>')
    expect(links(html)).toEqual([['/', 'marin GCS'], ['/hot', 'catalog-only preview'], ['/', 'storage map']])
    expect(roots.map(row => row.b)).toEqual([14, 8])
  } finally { client.clear() }
})
it('failed request shows a nonzero-result error with no maps or table', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retryOnMount: false } } })
  try {
    client.getQueryCache().build(client, { queryKey: ['name-summary', '2026-10-05', 'datakit', undefined] }, { data: undefined, dataUpdatedAt: 0, dataUpdateCount: 0,
      error: new Error('Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'), errorUpdatedAt: 1, errorUpdateCount: 1,
      fetchFailureCount: 1, fetchFailureReason: null, fetchMeta: null, isInvalidated: false, status: 'error', fetchStatus: 'idle' })
    const html = render(client)
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'])
    expect([...html.matchAll(/<table\b/g)]).toEqual([]); expect(roots).toEqual([])
  } finally { client.clear() }
})
