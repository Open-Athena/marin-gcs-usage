import { afterEach, describe, expect, it, vi } from 'vitest'
import { HOT_SCOPE, HotUnavailable, hotDraft, hotDraftParams } from './hotModel'
import { hotBucketHref, hotDrillRequest, loadHotDrill, parseHotDrill } from './hotDrillModel'

const request = { date: '2026-10-05', name: '.json', path: 'bucket-a' }
function fixture() {
  return { schema: 'hot-l2-pair-catalog-v1', target: 'fixture', date: request.date, pattern: request.name, path: request.path,
    exact: true, incremental: false, levels: 2, scope: HOT_SCOPE, source: 'registered accepted paired sparse artifact',
    geometry: { pre: 1, post: 10 }, cutoff: { scope: 'fixed per query and bucket root across all depth-2 children; not arbitrary-prefix refinement', dates: ['2026-10-04', '2026-10-05'], budget: 64, threshold_bytes: '5' },
    validation: { artifact_sha256: 'a'.repeat(64), artifact_bytes: 123, prefix_proofs_checked: true, full_catalog_source_oracle: false,
      covered_control: { pattern: 'm', validation: 'complete ordinary recursive bucket/frame rollups on both dates', frames_checked: 3, heavy_cells_checked: 2, buckets_checked: 6 },
      query_oracle: { predicate_id: 1, pattern: '.json', checked: false, reason: 'no emitted cell within interval cap' } },
    capabilities: { bucket_roots_only: true, child_drill: false, filters: false }, root: { b: '8', o: '5' },
    children: [{ path: 'bucket-a/folder', name: 'folder', pre: 2, post: 4, b: '6', o: '2', drill: false }], other: { b: '2', o: '3', drill: false } }
}
const expected = { after: { date: request.date, pattern: request.name, path: request.path, root: { b: 8, o: 5 },
  children: [{ path: 'bucket-a/folder', name: 'folder', pre: 2, post: 4, b: 6, o: 2 }], other: { b: 2, o: 3 } }, threshold: 5, budget: 64 }
function diffFixture() {
  const v = fixture()
  const pair = (before: { b: string; o: string }, after: { b: string; o: string }) => ({ before, after,
    delta: { b: String(BigInt(after.b) - BigInt(before.b)), o: String(BigInt(after.o) - BigInt(before.o)) } })
  return { ...v, schema: 'hot-l2-pair-catalog-diff-v1', dates: ['2026-10-04', request.date],
    root: pair({ b: '14', o: '7' }, v.root), children: [{ ...v.children[0], ...pair({ b: '12', o: '2' }, v.children[0]) }],
    other: { drill: false, ...pair({ b: '2', o: '5' }, v.other) } }
}
afterEach(() => vi.unstubAllGlobals())
describe('exact sparse bucket partition', () => {
  it('parses canonical decimal weights without changing labels or inventing a match requirement', () => {
    expect(parseHotDrill(fixture(), request)).toEqual(expected)
  })
  it('validates every dated delta and exact small counterparts in the fixed partition', () => {
    const v = diffFixture()
    v.children[0].after = { b: '0', o: '2' }; v.children[0].delta = { b: '-12', o: '0' }
    v.root.after = { b: '2', o: '5' }; v.root.delta = { b: '-12', o: '-2' }
    expect(parseHotDrill(v, { ...request, from: '2026-10-04' })).toEqual({ ...expected,
      before: { ...expected.after, date: '2026-10-04', root: { b: 14, o: 7 }, children: [{ ...expected.after.children[0], b: 12 }], other: { b: 2, o: 5 } },
      after: { ...expected.after, root: { b: 2, o: 5 }, children: [{ ...expected.after.children[0], b: 0 }] } })
  })
  it.each(['01', '-0', '+1', '1.0', '9007199254740992'])('refuses unsupported unsigned decimal %s', b => {
    const v = fixture(); v.root.b = b
    expect(() => parseHotDrill(v, request)).toThrow('Bucket detail returned an unsupported or inconsistent exact partition.')
  })
  it.each(['partition', 'geometry', 'label', 'drill', 'proof', 'request', 'cutoff'])('refuses inconsistent %s', issue => {
    const v = fixture()
    if (issue === 'partition') v.other.o = '4'
    if (issue === 'geometry') v.children[0].pre = 1
    if (issue === 'label') v.children[0].path = 'bucket-a/elsewhere'
    if (issue === 'drill') v.children[0].drill = true
    if (issue === 'proof') v.validation.prefix_proofs_checked = false
    if (issue === 'request') v.pattern = '.npy'
    if (issue === 'cutoff') v.cutoff.dates.reverse()
    expect(() => parseHotDrill(v, request)).toThrow('Bucket detail returned an unsupported or inconsistent exact partition.')
  })
  it('refuses a changed child delta even when the root is correct', () => {
    const v = diffFixture(); v.children[0].delta.b = '-5'
    expect(() => parseHotDrill(v, { ...request, from: '2026-10-04' })).toThrow('Bucket detail returned an unsupported or inconsistent exact partition.')
  })
  it('accepts the covered-control declaration independent of object key ordering', () => {
    const v = fixture(), c = v.validation.covered_control
    const body = { ...v, pattern: 'm', validation: { ...v.validation,
      query_oracle: { buckets_checked: c.buckets_checked, heavy_cells_checked: c.heavy_cells_checked, frames_checked: c.frames_checked, validation: c.validation, pattern: c.pattern } } }
    expect(parseHotDrill(body, { ...request, name: 'm' })).toEqual({ ...expected, after: { ...expected.after, pattern: 'm' } })
  })
  it('rejects overlapping duplicate/unknown geometry even if totals were rebalanced', () => {
    const v = fixture(); v.children.push({ ...v.children[0], name: 'other-child', path: 'bucket-a/other-child', b: '0', o: '0' })
    expect(() => parseHotDrill(v, request)).toThrow('Bucket detail returned an unsupported or inconsistent exact partition.')
  })
})
describe('bounded navigation and unavailable semantics', () => {
  it('preserves normalized query/dates in bucket links and removes only path for the crumb; search clears it', () => {
    const p = new URLSearchParams('name=.JSON&date=2026-10-05&from=2026-10-04&path=bucket-a')
    const r = hotDrillRequest(p)
    expect(r).toEqual({ ...request, from: '2026-10-04' })
    expect(hotBucketHref(r, 'bucket-b')).toBe('/hot?name=.json&date=2026-10-05&from=2026-10-04&path=bucket-b')
    expect(hotBucketHref(r)).toBe('/hot?name=.json&date=2026-10-05&from=2026-10-04')
    expect(hotDraftParams(hotDraft(p)).toString()).toBe('name=.JSON&date=2026-10-05&from=2026-10-04')
  })
  it.each(['path=', 'path=a/b', 'path=a&path=b', 'path=%00'])('refuses bucket identity %s', path => {
    expect(() => hotDrillRequest(new URLSearchParams(`name=.json&date=2026-10-05&${path}`))).toThrow('Choose one bucket; deeper paths are not available.')
  })
  it('requests only L2 once, returns exact data and never falls back', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture())))
    vi.stubGlobal('fetch', fetcher)
    expect(await loadHotDrill(request)).toEqual(expected)
    expect(fetcher.mock.calls).toEqual([['/api/hot-l2?name=.json&date=2026-10-05&path=bucket-a', { signal: undefined }]])
  })
  it.each([400, 404, 409, 422])('unavailable %s is not an empty result', async status => {
    const fetcher = vi.fn().mockResolvedValue(new Response('private details', { status })); vi.stubGlobal('fetch', fetcher)
    await expect(loadHotDrill(request)).rejects.toEqual(new HotUnavailable('Bucket detail is unavailable for this literal/date. It is not a zero-match result; return to All buckets for the root summary.'))
    expect(fetcher.mock.calls.length).toBe(1)
  })
})
