import { beforeAll, describe, expect, it } from 'vitest'
import { answerKey, type Store } from './nameSummaryStatic.js'
import { catalogAnswer } from './staticCatalog.js'
import { liveTotal, SuffixHits } from './staticFilter.js'
import type { Blobs } from './staticNames.js'
import { latestManifest, tiers } from './staticRuns.js'
import { fixture } from './testStore.js'

// The daily runs' reader (`staticRuns.ts`) over `fixtures/static-runs/` (`gen.py`): a base through 2026-09-01 and
// a run per scan — 2026-10-01, 2026-10-02 (date ids), then two scans on one day, 2026-10-03T0600 and 2026-10-03T1800
// (scan ids, specs/scan-ids-not-dates.md) — against a brute-force oracle over every version on every scan.

const RUNS = ['2026-10-01', '2026-10-02', '2026-10-03T0600', '2026-10-03T1800']
const TIERS = ['base', ...RUNS.map(r => `deltas/${r}`)]
const FILES = ['shards.json', 'sx/s0000.parquet', 'sx/s0001.parquet', 'catalog/cells.parquet', 'catalog/index.parquet', 'catalog/meta.json']
const DATES = ['2026-08-01', '2026-09-01', ...RUNS]
const MANIFESTS = RUNS.map(r => `manifests/${r}.json`)
const held = new Map<string, ArrayBuffer>()
let expected: Record<string, Record<string, Record<string, [number, number]>>>
let catalog: Record<string, [string, number, number, number][] | null>
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  const keys = [...TIERS.flatMap(t => FILES.map(f => `${t}/${f}`)), 'base/scans.json', ...MANIFESTS, 'expected.json', 'catalog-expected.json']
  for (const k of keys) { const b = fs.readFileSync(fixture(`static-runs/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  const text = (k: string) => JSON.parse(new TextDecoder().decode(held.get(k)))
  expected = text('expected.json')
  catalog = text('catalog-expected.json')
})

/** The fixture as one generation's `Blobs` (the base at the root, runs under `deltas/`), listing only the
 *  manifests in `visible`, recording each read. */
function files(visible: string[] = MANIFESTS, log: string[] = []): Blobs {
  const path = (key: string) => key.startsWith('deltas/') || key.startsWith('manifests/') ? key : `base/${key}`
  const bytes = (key: string) => { const b = held.get(path(key)); if (!b) throw new Error(`no fixture ${key}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); const end = length == null ? b.byteLength : offset + length; log.push(`${key}@${offset}+${end - offset}`); return b.slice(offset, end) },
    async suffix(key, n) { const b = bytes(key), size = b.byteLength; log.push(`${key}@-${n}`); return { buf: b.slice(Math.max(0, size - n)), size } },
    async json(key) { log.push(key); return JSON.parse(new TextDecoder().decode(bytes(key))) },
    async list(prefix) { log.push(`list ${prefix}`); return visible.filter(k => k.startsWith(prefix)) },
  }
}
const num = (answers: Record<string, Record<string, [bigint, bigint]>>) =>
  Object.fromEntries(Object.entries(answers).map(([d, t]) => [d, Object.fromEntries(Object.entries(t).filter(([, [b, o]]) => b !== 0n || o !== 0n).map(([b, [x, o]]) => [b, [Number(x), Number(o)]]))]))
const terms = () => Object.keys(expected)
const long = () => terms().filter(t => [...t].length >= 3)
const store = (blobs: Blobs): Store => { const t = tiers(blobs); return { names: t.names, catalog: t.catalog, scans: t.scans, clock: async () => Date.now() } }
const until = (date: string) => Object.fromEntries(DATES.filter(d => d <= date).map(d => [d, null]))

describe('static runs', () => {
  it('takes the newest manifest; without one (or without `list`) the base alone', async () => {
    expect((await latestManifest(files()))?.runs.map(r => r.key)).toEqual(TIERS.slice(1))
    expect((await latestManifest(files([MANIFESTS[0]])))?.runs.map(r => r.key)).toEqual(['deltas/2026-10-01'])
    expect(await latestManifest(files([]))).toBeNull()
    const { list: _, ...listless } = files()
    expect(await latestManifest(listless)).toBeNull()
    expect(await tiers(files()).scans()).toEqual(DATES)
    expect(await tiers(files([MANIFESTS[0]])).scans()).toEqual(DATES.slice(0, 3))
    expect(await tiers(files([])).scans()).toEqual(DATES.slice(0, 2))
  })

  it('exercises close records, opens, and a literal that became a member in the last run', () => {
    expect(catalog.qqq?.length).toBeGreaterThan(1)
    expect(expected.foo['2026-10-02']).not.toEqual(expected.foo['2026-10-01'])
    expect(expected['late']['2026-10-01']).toEqual({ 'bkt-b': [43, 2] })
    expect(expected['late']['2026-10-02']).toEqual({ 'bkt-b': [21, 1] })
  })

  it.each([['2026-10-03T1800', MANIFESTS], ['2026-10-03T0600', MANIFESTS.slice(0, 3)], ['2026-10-02', MANIFESTS.slice(0, 2)],
    ['2026-10-01', [MANIFESTS[0]]], ['2026-09-01', []]] as const)(
    'answers every literal of 3+ characters like the oracle on every date through %s', async (last, visible) => {
      const t = tiers(files([...visible]))
      const dates = Object.keys(until(last))
      for (const term of long()) {
        const { answer } = await t.names.answer(term, dates)
        expect([term, num(answer!.answers)]).toEqual([term, Object.fromEntries(dates.map(d => [d, expected[term][d]]))])
      }
    })

  it('looks up every literal in the merged catalog: the newest header, every tier\'s cells', async () => {
    const t = tiers(files())
    for (const term of terms()) {
      const { member } = await t.catalog.lookup(term)
      const want = catalog[term]
      if (!want) { expect([term, member]).toEqual([term, null]); continue }
      expect([term, member!.rows, member!.cells.map(c => [c.bucket, Number(c.vf), Number(c.b), Number(c.o)])])
        .toEqual([term, want[0][2], want.slice(1)])
      const ans = catalogAnswer(member!, DATES)
      expect([term, num(ans)]).toEqual([term, expected[term]])
    }
  })

  it('answers every literal through the summary dispatch (catalog or bounded postings) like the oracle', async () => {
    const s = store(files())
    for (const term of terms()) {
      const { answers } = await answerKey(s, term, DATES)
      expect([term, num(answers)]).toEqual([term, expected[term]])
    }
  })

  it('folds the map filter\'s hits over the tiers: per bucket root, the live totals equal the oracle\'s', async () => {
    const t = tiers(files())
    const src = new SuffixHits(t.names)
    for (const term of long()) {
      const got = await src.all(term)
      for (const d of DATES) {
        const per = Object.fromEntries(['bkt-a', 'bkt-b'].map(b => {
          const { b: x, o } = liveTotal(got!.hits.filter(h => h.path === b || h.path.startsWith(`${b}/`)), d)
          return [b, [x, o]] as const
        }).filter(([, [x, o]]) => x !== 0 || o !== 0))
        expect([term, d, per]).toEqual([term, d, expected[term][d]])
      }
    }
  })

  it('keys runs by scan id: two scans on one day are two runs, two manifests, two answers', async () => {
    expect((await latestManifest(files()))?.date).toEqual('2026-10-03T1800')
    expect((await latestManifest(files(MANIFESTS.slice(0, 3))))?.date).toEqual('2026-10-03T0600')
    expect(await tiers(files(MANIFESTS.slice(0, 3))).scans()).toEqual(DATES.slice(0, 5))
    const am = '2026-10-03T0600', pm = '2026-10-03T1800'
    const { answer } = await tiers(files()).names.answer('sub-', ['2026-10-02', am, pm])
    expect(num(answer!.answers)).toEqual({ '2026-10-02': {}, [am]: { 'bkt-a': [7, 1] }, [pm]: { 'bkt-b': [9, 1] } })
    const hits = (await new SuffixHits(tiers(files()).names).all('sub-'))!.hits
    expect([am, pm].map(d => liveTotal(hits, d))).toEqual([{ b: 7, o: 1, roots: 1 }, { b: 9, o: 1, roots: 1 }])
    expect(expected['sub-']).toEqual({ ...Object.fromEntries(DATES.slice(0, 4).map(d => [d, {}])), [am]: { 'bkt-a': [7, 1] }, [pm]: { 'bkt-b': [9, 1] } })
  })

  it('picks up a new manifest after the TTL: the hit lists are held per version', async () => {
    const visible = [MANIFESTS[0]]
    let now = 0
    const blobs: Blobs = { ...files(visible), list: async p => visible.filter(k => k.startsWith(p)) }
    const t = tiers(blobs, { ttlMs: 1000, now: () => now })
    const src = new SuffixHits(t.names)
    const total = async () => liveTotal((await src.all('qqq'))!.hits, '2026-10-02')
    expect(await t.scans()).toEqual(DATES.slice(0, 3))
    expect(await total()).toEqual({ b: 0, o: 0, roots: 0 })
    visible.push(MANIFESTS[1])
    expect(await total()).toEqual({ b: 0, o: 0, roots: 0 }) // within the TTL: the held state
    now = 1000
    expect(await t.scans()).toEqual(DATES.slice(0, 4))
    expect(await total()).toEqual({ b: 30 + 31 + 32 + 33 + 34, o: 5, roots: 5 })
  })
})
