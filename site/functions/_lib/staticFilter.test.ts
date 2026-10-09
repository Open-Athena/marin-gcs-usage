import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { type OwnerScope, parseQuery } from './scope'
import { type Hit, injectedStores, liveTotal, type HitSource, SuffixHits, staticLiteral, type StaticFilterStore } from './staticFilter'
import { type Blobs, StaticNames } from './staticNames'
import { parseAst } from './querySyntax'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type ViewNode, type View } from './view'
import { searchKey } from './search'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The map's filter from the static name index (`staticFilter.ts`) over `fixtures/static-filter/`
// (`gen.py`): two scans (A, B) of buckets `bk` (3000 filler objects + `tomat` roots at every depth, two
// owners' slices under one dir, objects changing between the scans), `tomato-bk` (a bucket the term
// matches) and `zz`, each a store generation WITH the search sidecars, plus the static index over both.
// The same views are built twice — static (`STATIC_FILTER_STORE`) and from the search sidecars, both
// exact — and checked equal to each other and to brute force from the objects (`expected.json`).

const A = '2026-10-04'
const B = '2026-10-05'
const DATES = [A, B]
const TERMS = ['tomat', 'ttl', 'ckpt', 'safetensors', 'x.bin', 'nomatch']
const ROOTS = ['', 'bk', 'bk/data', 'bk/data/raw', 'bk/data/tomato', 'zz', 'tomato-bk', 'bk/runs/x']
const W = 1280, H = 768, MIN_AREA = 12, ATTEN = 2
const dirOf = (date: string) => `cw-l2/${date}/index/g`
const KEYS = ['shards.json', 'scans.json', 'sx/s0000.parquet', 'sx/s0001.parquet']

type Expected = { roots: Record<string, Record<string, Record<string, [string, number, number][] | null>>>; series: Record<string, Record<string, Record<string, [number, number]>>> }
let expected: Expected
let base: Env
const held = new Map<string, ArrayBuffer>()
function blobs(): Blobs {
  const bytes = (k: string) => { const b = held.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
/** A static store over the fixture: `maxRows` and `heavy` as the dispatch's knobs. */
function staticStore(opts: { maxRows?: number; heavy?: HitSource | null } = {}): StaticFilterStore {
  return { source: new SuffixHits(new StaticNames(blobs()), opts), scans: async () => [...DATES], gen: 'fixture' }
}
const envStatic = (s: StaticFilterStore = staticStore()): Env => { const env = { ...base }; injectedStores.set(env, s); return env }

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of KEYS) { const b = fs.readFileSync(fixture(`static-filter/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  expected = await readJson<Expected>('static-filter/expected.json')
  const { db, raw } = await sqliteD1('cw')
  for (const date of DATES) {
    const v = await readJson<Record<string, D1Variant>>(`static-filter/${date}/d1.json`)
    const files = {
      path: { parquet: `static-filter/${date}/path-index.parquet`, groups: `static-filter/${date}/path-index.groups.json` },
      bysize: { parquet: `static-filter/${date}/path-index-bysize.parquet`, groups: `static-filter/${date}/path-index-bysize.groups.json` },
    }
    seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
    for (const [role, file] of [['rows', 'rows'], ['trigrams', 'trigrams'], ['rowsSearch', 'rows-search']] as const) {
      FILES.set(searchKey(dirOf(date), role), fixture(`static-filter/${date}/path-index.${file}.parquet`))
    }
  }
  base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
})

const q = (t: string) => parseQuery(t)!
const view = (env: Env, date: string, path: string, t: string, extra: Partial<Parameters<typeof buildView>[1]> = {}) =>
  buildView(env, { date, path, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, query: q(t), ...extra })
/** The view with its tier's phase-1 name dropped (`static+bysize` ~ `search+bysize`). */
const sansP1 = (v: View) => ({ ...v, tier: v.tier.replace(/^(static|search)\+/, '+') })
/** `matched` as brute force's `[path, bytes, objects]`, by path. */
const matchedRows = (v: { matched?: { path: string; b: number; o: number }[] }) =>
  (v.matched ?? []).map(m => [m.path, m.b, m.o]).sort((x, y) => (x[0] < y[0] ? -1 : 1))

describe('staticLiteral: the queries the static index answers exactly', () => {
  it('one positive substring of ≥ 3 characters without `/`; nothing else', () => {
    const lit = (s: string) => staticLiteral(parseAst(s)!)
    expect(['tomat', 'TOMAT', 'x.bin', 'abc def', 'tom*at', '/tomat', 'tomat -xyz', 'abc|bcd', '"ttl=7d"', '/ttl/'].map(lit))
      .toEqual(['tomat', 'tomat', 'x.bin', null, null, null, null, null, 'ttl=7d', null])
  })
})

describe('static filter: the treemap / table view (`/api/subtree`)', () => {
  for (const date of DATES) for (const root of ROOTS) {
    it(`= the search-sidecar view and brute force, under '${root}' on ${date}`, async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) {
        const [s, r] = await Promise.all([view(envStatic(), date, root, t), view(base, date, root, t)])
        const brute = expected.roots[t][root][date]
        got.push([t, sansP1(s), brute === null ? 'whole' : matchedRows(s)])
        want.push([t, sansP1(r), brute === null ? 'whole' : brute])
        // Served statically (no approximate note) wherever the root doesn't match and anything does.
        if (brute !== null && brute.length) expect([t, s.tier.split('+')[0], s.approximate, s.partial]).toEqual([t, 'static', undefined, undefined])
      }
      expect(got).toEqual(want)
    })
  }

  it('the first paint: the match roots alone, exact, as leaves under their ancestors', async () => {
    const leaves = (n: ViewNode, path: string, out: [string, number, number][] = []): [string, number, number][] => {
      if (n.m) { expect(n.c).toBeUndefined(); out.push([path, n.b, n.o]) }
      for (const c of n.c ?? []) leaves(c, path ? `${path}/${c.n}` : c.n, out)
      return out
    }
    for (const date of DATES) for (const root of ['', 'bk', 'bk/data']) {
      const v = await view(envStatic(), date, root, 'tomat', { firstPaint: true })
      const brute = expected.roots.tomat[root][date]!
      expect([date, root, leaves(v.tree, root).sort((x, y) => (x[0] < y[0] ? -1 : 1)), v.tree.b, v.tier.split('+')[0]]).toEqual([date, root, brute, brute.reduce((n, x) => n + x[1], 0), 'static'])
    }
  })

  it('an owner pool: the same as the search view', async () => {
    for (const owner of ['owned', 'unowned', { not: ['alice'] }] as OwnerScope[]) for (const root of ['', 'bk/data']) {
      const [s, r] = await Promise.all([view(envStatic(), A, root, 'tomat', { owner }), view(base, A, root, 'tomat', { owner })])
      expect([owner, root, sansP1(s)]).toEqual([owner, root, sansP1(r)])
    }
  })
})

describe('static filter: the diff view (`/api/diff`)', () => {
  for (const root of ROOTS) {
    it(`= the search-sidecar diff, under '${root}'`, async () => {
      const got: unknown[] = []
      const want: unknown[] = []
      for (const t of TERMS) for (const extra of [{}, { depth: 1 }, { summary: true }]) {
        const o = { from: A, to: B, path: root, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 200, query: q(t), ...extra }
        const [s, r] = await Promise.all([buildDiff(envStatic(), o), buildDiff(base, o)])
        got.push([t, extra, { ...s, tier: s.tier.replace(/^(static|search)\+/, '+') }])
        want.push([t, extra, { ...r, tier: r.tier.replace(/^(static|search)\+/, '+') }])
      }
      expect(got).toEqual(want)
    })
  }

  it('its matched roots are both scans\' brute-force roots', async () => {
    const d = await buildDiff(envStatic(), { from: A, to: B, path: '', w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 200, query: q('tomat') })
    const union = new Map<string, [string, number, number]>()
    for (const date of DATES) for (const r of expected.roots.tomat[''][date]!) union.set(r[0], r)
    // Each root once, with the newer scan's numbers where both have it.
    expect(matchedRows(d).map(r => r[0])).toEqual([...union.keys()].sort())
  })
})

describe('static filter: size over time (`/api/series`)', () => {
  it('Σ each scan\'s own match roots under the path = brute force', async () => {
    const src = staticStore().source
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of TERMS) for (const root of ROOTS) {
      if (expected.roots[t][root][A] === null) continue
      const { hits } = (await src.hits(t, root))!
      got.push([t, root, Object.fromEntries(DATES.map(d => { const x = liveTotal(hits, d); return [d, [x.b, x.o]] }))])
      want.push([t, root, expected.series[t][root]])
    }
    expect(got).toEqual(want)
  })
})

describe('static filter: dispatch', () => {
  it('a range over `maxRows` asks the heavy source; none → today\'s read (the search sidecars)', async () => {
    const asked: [string, string][] = []
    const none: HitSource = { async hits(key, under) { asked.push([key, under]); return null } }
    const v = await view(envStatic(staticStore({ maxRows: 3, heavy: none })), A, 'bk', 'tomat')
    expect([asked, v.tier.split('+')[0], matchedRows(v)]).toEqual([[['tomat', 'bk']], 'search', expected.roots.tomat.bk[A]])
  })

  it('the heavy source\'s hits serve the view statically', async () => {
    const light = staticStore().source
    const heavy: HitSource = { hits: (key, under) => light.hits(key, under) }
    const v = await view(envStatic(staticStore({ maxRows: 3, heavy })), B, '', 'tomat')
    expect([v.tier.split('+')[0], matchedRows(v)]).toEqual(['static', expected.roots.tomat[''][B]])
  })

  it('a scan outside the generation, a lens or a class scope keep today\'s read', async () => {
    const outside: StaticFilterStore = { ...staticStore(), scans: async () => [B] }
    const v = await view(envStatic(outside), A, '', 'tomat')
    expect(v.tier.split('+')[0]).toBe('search')
    const c = await view(envStatic(), A, '', 'tomat', { classes: new Set(['1']) as never })
    expect(c.tier.split('+')[0]).toBe('search')
  })

  it('a first hit dedups a name holding the term twice (one row per occurrence in the shards)', async () => {
    const { hits } = (await staticStore().source.hits('tomat', 'bk/runs/x'))!
    const key = (h: Hit) => `${h.path} ${h.usr} ${h.vf}`
    expect(hits.map(key).sort()).toEqual([
      `bk/runs/x/ckpt-tomat.pt alice ${Date.UTC(2026, 9, 4)}`, `bk/runs/x/ckpt-tomat.pt alice ${Date.UTC(2026, 9, 5)}`, `bk/runs/x/tomat-tomat.bin  ${Date.UTC(2026, 9, 4)}`,
    ])
  })
})
