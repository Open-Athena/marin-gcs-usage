import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadName, nameHasDetail, namePageParams, nameRequest, parseName } from './nameModel'
import { nameDiff, nameFixture } from './nameTestFixtures'

const request = { date: '2026-10-05', name: 'datakit' }
const view = (body: ReturnType<typeof nameFixture>) => ({ target: 'fixture', date: body.date, pattern: 'datakit', root: body.root, buckets: body.buckets })
const execution = (body: ReturnType<typeof nameFixture>) => ({ plan: body.plan, source: body.source, validation: body.validation, source_identity: body.source_identity })
afterEach(() => vi.unstubAllGlobals())
describe('unified exact schemas and truthful plans', () => {
  it.each(['catalog', 'bounded-name-postings'] as const)('parses complete %s body without inventing a catalog schema or artifact identity', plan => {
    const body = nameFixture(plan)
    expect(parseName(body, request)).toEqual({ after: view(body), execution: { after: execution(body) } })
    expect(nameHasDetail(parseName(body, request))).toBe(plan === 'catalog')
  })
  it.each([['catalog', 'catalog', true], ['catalog', 'bounded-name-postings', false], ['bounded-name-postings', 'catalog', false], ['bounded-name-postings', 'bounded-name-postings', false]] as const)
    ('validates complete %s/%s comparisons and gates detail=%s', (before, after, detail) => {
      const body = nameDiff(before, after), r = { ...request, from: '2026-10-04' }, result = parseName(body, r)
      expect(result).toEqual({ before: view(body.before), after: view(body.after), delta: { b: -6, o: -2 }, execution: { before: execution(body.before), after: execution(body.after) } })
      expect(nameHasDetail(result)).toBe(detail)
    })
  it.each(['unknown plan', 'missing proof', 'false proof', 'source oracle claim', 'empty source', 'empty description', 'cold catalog metadata', 'invalid identity'])('refuses %s execution metadata', issue => {
    const body = nameFixture()
    if (issue === 'unknown plan') Object.assign(body, { plan: 'full-fleet-scan' })
    if (issue === 'missing proof') Object.assign(body, { validation: {} })
    if (issue === 'false proof') body.validation.source_prefix_proofs_checked = false
    if (issue === 'source oracle claim') body.validation.independent_query_source_oracle = true
    if (issue === 'empty source') body.source = ''
    if (issue === 'empty description') body.validation.description = ''
    if (issue === 'cold catalog metadata') Object.assign(body.validation, { catalog: {} })
    if (issue === 'invalid identity') body.source_identity.history_manifest_sha256 = 'bad'
    expect(() => parseName(body, request)).toThrow('Name summary returned invalid execution metadata.')
  })
  it('rejects comparisons across different source generations', () => {
    const body = nameDiff(); body.before.source_identity.generation = 'other'
    expect(() => parseName(body, { ...request, from: '2026-10-04' })).toThrow('Name summary comparison has inconsistent frozen source identities.')
  })
  it.each(['hot schema', 'partial buckets', 'wrong root', 'wrong literal', 'rounded integer', 'wrong delta', 'wrong geometry'])('rejects %s rather than returning partial totals', issue => {
    const body = nameDiff()
    if (issue === 'hot schema') body.schema = 'hot-l1-batch-catalog-diff-v1'
    if (issue === 'partial buckets') body.after.buckets.pop()
    if (issue === 'wrong root') body.after.root.o++
    if (issue === 'wrong literal') body.before.pattern = '.json'
    if (issue === 'rounded integer') body.after.root.b = 9007199254740992
    if (issue === 'wrong delta') body.buckets[0].delta.b = -5
    if (issue === 'wrong geometry') body.buckets[0].post++
    expect(() => parseName(body, { ...request, from: '2026-10-04' })).toThrow(Error)
  })
})
describe('requests and explicit failures', () => {
  it('defaults to datakit without rewriting explicit names or baseline controls', () => {
    expect(namePageParams(new URLSearchParams()).toString()).toBe('name=datakit')
    expect(nameRequest(new URLSearchParams())).toEqual(request)
    expect(nameRequest(new URLSearchParams('name=.JSON&date=2026-10-05&from=2026-10-04'))).toEqual({ date: request.date, name: '.json', from: '2026-10-04' })
  })
  it('fetches exactly the unified endpoint and preserves metadata', async () => {
    const body = nameFixture(), fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)))
    vi.stubGlobal('fetch', fetcher)
    expect(await loadName({ ...request, name: 'DATAKIT' })).toEqual({ after: view(body), execution: { after: execution(body) } })
    expect(fetcher.mock.calls).toEqual([['/api/name-summary?date=2026-10-05&name=datakit', { signal: undefined }]])
  })
  it('invalid dates fail before any request', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    await expect(loadName({ ...request, date: '2026-10-06' })).rejects.toEqual(new Error('Only the frozen 2026-10-04 and 2026-10-05 scans are available.'))
    expect(fetcher.mock.calls).toEqual([])
  })
  it.each([[400, 'Invalid name-summary request. Check the literal and frozen scan dates; this is not a zero-match result.'], [503, 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'], [401, 'Sign in to view name summaries.']] as const)('status %s never becomes an empty result or scan fallback', async (status, message) => {
    const fetcher = vi.fn().mockResolvedValue(new Response('private backend details', { status })); vi.stubGlobal('fetch', fetcher)
    await expect(loadName(request)).rejects.toEqual(new Error(message))
    expect(fetcher.mock.calls.length).toBe(1)
  })
})
