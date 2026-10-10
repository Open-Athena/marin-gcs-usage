import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parseQuery } from './scope'
import { Drill, type DrillAnswer, rollupAt, rollupTotal } from './staticDrill'
import { covers, drillSource, type Found, type HitSource, injectedStores, liveTotal, SuffixHits, type StaticFilterStore } from './staticFilter'
import { catalogAnswer, StaticCatalog } from './staticCatalog'
import { FilterRejected, REJECT_MESSAGES } from './indexedOnly'
import { type Blobs, type Hit, scanAt, StaticNames } from './staticNames'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type DiffRow, NotFound, type View, type ViewNode } from './view'
import { SEARCH_LIMITS, type SearchLimits, searchKey } from './search'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The heavy terms' drilldown (`staticDrill.ts`) over `fixtures/static-drill/` (`gen.py`): the `drill/` files
// and base catalog `dt_cloud.static_roots` builds from the two scans of `fixtures/static-filter/` (R = 6 root
// rows, K = 3 kept children, 4-row data groups, 3-entry index row groups; long members over two shard
// files, aliases; short members), checked against brute force from the objects (`expected.json`) — the
// reader alone, then the map's views served from it (the static-filter fixture's store generations).

const A = '2026-10-04'
const B = '2026-10-05'
const DATES = [A, B]
const LONG = ['tomat', 'tomato', 'ttl', 'ckpt', 'ckpt-', 'safetensor', 'safetensors', 'afetensors', 'bin', '.bin', 'x.bin', 'f00', 'fil', 'data', 'nomatch']
const SHORT = ['0', '.', 'f', 'a', 'x', 'om', 't', 'qq']
const TERMS = [...LONG, ...SHORT]
const W = 1280, H = 768, MIN_AREA = 12, ATTEN = 2
const dirOf = (date: string) => `cw-l2/${date}/index/g`

type Sums = Record<string, [number, number]>
type Expected = {
  paths: string[]
  aliases: Record<string, string>
  views: Record<string, Record<string, Record<string, Sums | null>>>
  roots: Record<string, Record<string, Record<string, [string, number, number][] | null>>>
}
let expected: Expected
let base: Env
const drillFiles = new Map<string, ArrayBuffer>()
const filterFiles = new Map<string, ArrayBuffer>()

/** `Blobs` over held files; `reads` collects every key read. */
function blobsOf(files: Map<string, ArrayBuffer>, reads?: string[], override: Record<string, unknown> = {}): Blobs {
  const bytes = (k: string) => { reads?.push(k); const b = files.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return key in override ? override[key] : JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
const drillBlobs = (reads?: string[], override?: Record<string, unknown>) => blobsOf(drillFiles, reads, override)
/** The drill alone (keys under `drill/`), with the base catalog for the fleet root. */
const newDrill = (reads?: string[]) => (drillSource(drillBlobs(reads)) as unknown as { drill: Drill }).drill

/** A static store over the static-filter fixture's suffix index, every ≥ 3-character literal over `maxRows`
 *  (0) so it asks the drilldown, as a catalog member does. */
function heavyStore(heavy: HitSource | null = drillSource(drillBlobs())): StaticFilterStore {
  return { source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 0, heavy }), scans: async () => [...DATES], gen: 'fixture' }
}
const envStatic = (s: StaticFilterStore = heavyStore()): Env => { const env = { ...base }; injectedStores.set(env, s); return env }

async function readTree(dir: string, into: Map<string, ArrayBuffer>, rel = ''): Promise<void> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
  for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
    const k = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) await readTree(`${dir}/${e.name}`, into, k)
    else { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); into.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  }
}

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  await readTree('static-drill', drillFiles)
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of ['shards.json', 'scans.json', 'sx/s0000.parquet', 'sx/s0001.parquet']) {
    const b = fs.readFileSync(fixture(`static-filter/${k}`))
    filterFiles.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer)
  }
  expected = await readJson<Expected>('static-drill/expected.json')
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

const n = (x: bigint) => Number(x)
/** Live roots summed per child of `P` on `date` (zeros dropped, by name). */
function childSums(hits: Hit[], P: string, date: string): Sums {
  const D = scanAt(date), acc = new Map<string, [number, number]>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt)) continue
    const c = (P === '' ? h.path : h.path.slice(P.length + 1)).split('/')[0]
    const e = acc.get(c) ?? [0, 0]
    acc.set(c, [e[0] + n(h.size), e[1] + n(h.n)])
  }
  return Object.fromEntries([...acc].filter(([, [b, o]]) => b || o).sort(([x], [y]) => (x < y ? -1 : 1)))
}
const total = (s: Sums): [number, number] => Object.values(s).reduce<[number, number]>((t, [b, o]) => [t[0] + b, t[1] + o], [0, 0])
const keptOf = (a: DrillAnswer) => 'rollup' in a ? [...new Set(a.rollup.cells.filter(c => c.kind === 1).map(c => c.child))] : []

describe('Drill.view: the reader = brute force from the objects', () => {
  it('every term at every path on both scans: roots reads child for child, rollups kept children + remainder', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    const sources: Record<string, number> = {}
    for (const t of TERMS) for (const P of expected.paths) {
      const a = await drill.view(t, P)
      sources[a.source] = (sources[a.source] ?? 0) + 1
      for (const d of DATES) {
        const exp = expected.views[t][P][d]
        if (a.source === 'plain' || a.source === 'none') {
          got.push([t, P, d, a.source])
          want.push([t, P, d, exp === null ? 'plain' : Object.keys(exp).length ? 'members only' : 'none'])
        } else if (a.source === 'roots') {
          got.push([t, P, d, childSums(a.hits, P, d)])
          want.push([t, P, d, exp])
        } else {
          const { kids, rest } = rollupAt(a.rollup, d)
          const kept = keptOf(a)
          got.push([t, P, d, Object.fromEntries(kids.map(([c, b, o]) => [c, [n(b), n(o)]])), [n(rest[0]), n(rest[1])]])
          const keptExp = Object.fromEntries(Object.entries(exp!).filter(([c]) => kept.includes(c)))
          const all = total(exp!), k = total(keptExp)
          want.push([t, P, d, keptExp, [all[0] - k[0], all[1] - k[1]]])
        }
      }
    }
    expect(got).toEqual(want)
    // Every kind of answer occurs: 23 terms × 24 paths.
    expect(sources).toEqual({ plain: 58, none: 24, roots: 457, rollup: 6, catalog: 7 })
  })

  it('the dispatch: roots when the index bounds the range at ≤ R + 2 groups; else the true count is over R (a rollup)', async () => {
    const drill = newDrill()
    const meta = await drill.info()
    const bad: unknown[] = []
    for (const t of TERMS) for (const P of expected.paths) {
      const a = await drill.view(t, P)
      if (a.source === 'roots' && !(a.upper <= meta.dispatch_rows && a.hits.length <= a.upper)) bad.push([t, P, a.upper, a.hits.length])
      if (a.source === 'rollup' && !(a.upper > meta.dispatch_rows && a.rollup.rows! > meta.R && a.rollup.kept <= meta.K)) bad.push([t, P, a.upper, a.rollup.rows])
    }
    expect([meta.R, meta.K, meta.dispatch_rows, bad]).toEqual([6, 3, 14, []])
    const at = async (t: string, P: string) => { const a = await drill.view(t, P); return [t, P, a.source, 'rollup' in a ? [a.rollup.kept, a.rollup.children, a.rollup.rows] : a.source === 'roots' ? a.hits.length : null] }
    expect(await Promise.all([['0', 'bk'], ['0', 'bk/fill'], ['f00', 'bk/fill'], ['0', ''], ['tomat', 'bk'], ['tomat', ''], ['bin', 'bk/data'], ['.', 'bk']].map(([t, P]) => at(t, P)))).toEqual([
      ['0', 'bk', 'rollup', [1, 1, 3000]],
      ['0', 'bk/fill', 'rollup', [3, 3000, 3000]],
      ['f00', 'bk/fill', 'rollup', [3, 1000, 1000]],
      ['0', '', 'catalog', [1, 1, 3000]],
      ['tomat', 'bk', 'roots', 10],
      ['tomat', '', 'catalog', [3, 3, 13]],
      ['bin', 'bk/data', 'roots', 6],
      ['.', 'bk', 'rollup', [3, 5, 16]],
    ])
  })

  it('aliases: a member with another\'s root set reads the canonical\'s rows', async () => {
    const drill = newDrill()
    const al = await drill.aliases()
    expect(Object.fromEntries([...al].filter(([q, a]) => q !== a.c).map(([q, a]) => [q, a.c]))).toEqual(expected.aliases)
    const reads: string[] = []
    const a = await newDrill(reads).view('safetensors', 'bk')
    expect([a.source, 'c' in a ? a.c : null]).toEqual(['roots', 'afetensors'])
  })

  it('one or two characters read the short set, three or more the long', async () => {
    const files = async (t: string, P: string) => { const reads: string[] = []; await newDrill(reads).view(t, P); return [...new Set(reads.filter(k => !k.endsWith('.json')).map(k => k.replace(/^drill\//, '').split(/[-/]/)[0]))].sort() }
    expect([await files('0', 'bk/fill'), await files('om', 'bk'), await files('f00', 'bk/fill'), await files('tomat', 'bk')])
      .toEqual([['short'], ['short'], ['aliases.parquet', 'long'], ['aliases.parquet', 'long']])
  })

  it('inside: a path whose lowercase name holds the literal is the plain view; a long non-member and a missing short literal', async () => {
    const drill = newDrill()
    const got = await Promise.all([['tomat', 'bk/data/tomato'], ['om', 'tomato-bk/a'], ['TOMAT'.toLowerCase(), 'zz/Checkpoints/TOMAT'], ['nomatch', 'bk'], ['qq', 'bk']].map(async ([t, P]) => {
      const a = await drill.view(t, P)
      return [t, P, a.source, a.source === 'roots' ? a.hits.length : null]
    }))
    expect(got).toEqual([
      ['tomat', 'bk/data/tomato', 'plain', null],
      ['om', 'tomato-bk/a', 'plain', null],
      ['tomat', 'zz/Checkpoints/TOMAT', 'plain', null],
      ['nomatch', 'bk', 'none', null],
      ['qq', 'bk', 'roots', 0],
    ])
  })

  it('the series: a roots answer\'s live totals and a rollup\'s running sums = each scan\'s brute-force total', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of TERMS) for (const P of expected.paths) {
      const a = await drill.view(t, P)
      if (a.source === 'plain' || a.source === 'none') continue
      got.push([t, P, DATES.map(d => { const x = a.source === 'roots' ? liveTotal(a.hits, d) : rollupTotal(a.rollup, d); return [x.b, x.o] })])
      want.push([t, P, DATES.map(d => total(expected.views[t][P][d]!))])
    }
    expect(got).toEqual(want)
  })
})

describe('DrillSource: the filter\'s heavy source', () => {
  it('answers with the base scans; plain views and non-members decline', async () => {
    const src = drillSource(drillBlobs())
    const sum = (f: Found | null) => f == null ? null : [f.rollup ? 'rollup' : 'hits', f.scans]
    expect([sum(await src.hits('0', 'bk/fill')), sum(await src.hits('tomat', 'bk')), sum(await src.hits('tomat', 'bk/data/tomato')), sum(await src.hits('nomatch', ''))])
      .toEqual([['rollup', DATES], ['hits', DATES], null, null])
  })

  it('a scan past the drill base (the daily runs\' dates) is not covered: `covers` declines it', async () => {
    const src = drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))
    const f = (await src.hits('0', 'bk/fill'))!
    expect([covers(f, [A]), covers(f, [B]), covers(f, [A, B]), covers({ hits: [], io: {} }, [B])]).toEqual([true, false, false, true])
  })
})

describe('DrillSource: roots answers across isolates (the colo cache)', () => {
  it('a fresh isolate takes a roots answer from the cache, reading no drill file; rollups are not cached', async () => {
    const held = new Map<string, string>()
    const colo = {
      async match(url: string) { const b = held.get(url); return b == null ? undefined : new Response(b) },
      async put(url: string, r: Response) { held.set(url, await r.text()) },
    } as unknown as Cache
    const first: string[] = []
    const a = (await drillSource(drillBlobs(first), { cache: colo, prefix: 'g' }).hits('tomat', 'bk'))!
    const later: string[] = []
    const b = (await drillSource(drillBlobs(later), { cache: colo, prefix: 'g' }).hits('tomat', 'bk'))!
    const key = (h: Hit) => `${h.path} ${h.depth} ${h.usr} ${h.vf} ${h.vt} ${h.size} ${h.n}`
    // The colo also holds the drill's index tops and aliases (their own keys).
    const answers = () => [...held.keys()].filter(u => u.includes('/roots-v1/')).map(u => decodeURIComponent(u.split('/').pop()!))
    expect([answers(), first.some(k => k.includes('roots')), b.hits!.map(key), later.filter(k => k.includes('roots')), b.scans])
      .toEqual([['tomat\0bk.json'], true, a.hits!.map(key), [], DATES])
    await drillSource(drillBlobs(), { cache: colo, prefix: 'g' }).hits('0', 'bk/fill')
    expect(answers()).toEqual(['tomat\0bk.json'])
  })
})

// --- the map's views ------------------------------------------------------------------------------

const q = (t: string) => parseQuery(t)!
const view = (env: Env, date: string, path: string, t: string, extra: Partial<Parameters<typeof buildView>[1]> = {}) =>
  buildView(env, { date, path, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, query: q(t), ...extra })
/** The tree's top level: per child `[name, bytes, objects, m?]` (`(other)` included). */
const top = (v: View) => (v.tree.c ?? []).map(c => [c.n, c.b, c.o, ...(c.m ? ['m'] : [])])
/** A tree whose synthesized nodes — `(other)` folds and the ancestors holding only match roots — carry no
 *  written-time / read-day / class mix: a static view looks up only its drawn roots' own rows, so an
 *  ancestor of roots under the pixel budget (`bin` at the root: 12 roots, 4 drawn) has bytes, objects and
 *  owners, not those (the search view knows every root's row). Match roots and their insides keep them. */
const sansFoldDetail = (n: ViewNode, inRoot = false): ViewNode => {
  const { d: _d, a: _a, cb: _cb, ...rest } = n
  const keep = inRoot || !!n.m
  return { ...(keep ? n : rest), ...(n.c ? { c: n.c.map(c => sansFoldDetail(c, keep && c.n !== '(other)')) } : {}) } as ViewNode
}
const sansP1 = (v: View) => ({ ...v, tier: v.tier.replace(/^(drill|search)\+/, '+'), tree: sansFoldDetail(v.tree) })
const matchedRows = (v: { matched?: { path: string; b: number; o: number }[] }) =>
  (v.matched ?? []).map(m => [m.path, m.b, m.o]).sort((x, y) => (x[0] < y[0] ? -1 : 1))

describe('the map\'s views from the drilldown (`/api/subtree`)', () => {
  it('roots answers: the same view as the search sidecars\', and brute force\'s match roots', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    let n = 0
    for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) {
      const a = await drill.view(t, P)
      if (a.source !== 'roots' || !a.hits.length) continue
      for (const d of DATES) {
        const roots = expected.roots[t][P][d]
        if (!roots?.length) continue
        const [s, r] = await Promise.all([view(envStatic(), d, P, t), view(base, d, P, t)])
        n++
        got.push([t, P, d, s.tier.split('+')[0], sansP1(s), matchedRows(s)])
        want.push([t, P, d, 'drill', sansP1(r), roots])
      }
    }
    expect(got).toEqual(want)
    expect(n).toBe(256)
  }, 30_000) // ~1.5 s alone; past the 5 s default when the whole suite runs in parallel

  it('rollup answers: a cell per kept child (match roots marked), the rest in `(other)`; the matched totals exact', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) {
      const a = await drill.view(t, P)
      if (!('rollup' in a)) continue
      for (const d of DATES) {
        const exp = expected.views[t][P][d]!
        // Every child drawn (a 1-byte floor): the kept ones by name, the remainder as `(other)`.
        const v = await view(envStatic(), d, P, t, { threshold: 0.5 })
        const kept = keptOf(a)
        const keptExp = Object.entries(exp).filter(([c]) => kept.includes(c))
        const all = total(exp), k = total(Object.fromEntries(keptExp))
        const rest = [all[0] - k[0], all[1] - k[1]]
        const isRoot = (c: string) => c.toLowerCase().includes(t)
        got.push([t, P, d, v.tier, v.tree.b, v.tree.o, top(v), matchedRows(v), v.matchCount, v.matchesCapped, v.rollup])
        want.push([t, P, d, 'rollup', all[0], all[1],
          [...keptExp.map(([c, [b, o]]) => [c, b, o, ...(isRoot(c) ? ['m'] : [])]).sort((x, y) => (y[1] as number) - (x[1] as number)), ...(rest[0] > 0.5 ? [['(other)', rest[0], rest[1]]] : [])],
          keptExp.filter(([c]) => isRoot(c)).map(([c, [b, o]]) => [P === '' ? c : `${P}/${c}`, b, o]).sort((x, y) => ((x[0] as string) < (y[0] as string) ? -1 : 1)),
          { n: a.rollup.rows ?? keptExp.filter(([c]) => isRoot(c)).length, b: all[0], o: all[1] }, true, { children: a.rollup.children, kept: a.rollup.kept, rows: a.rollup.rows }])
      }
    }
    expect(got).toEqual(want)
    expect(got.length).toBe(26)
  })

  it('a kept child that is a match root carries its own row\'s kind (a file under `bk/fill` for `0`: the K = 3 largest peaks, ties by name)', async () => {
    const v = await view(envStatic(), A, 'bk/fill', '0', { threshold: 0.5 })
    expect(v.tree.c!.map(c => [c.n, c.k, c.b, c.o, c.m, c.f])).toEqual([
      ['f00011', 'file', 2048, 1, 1, undefined], ['f00023', 'file', 2048, 1, 1, undefined], ['f00035', 'file', 2048, 1, 1, undefined],
      // 3000 files of 1 << (i % 12) bytes: 250 × 4095.
      ['(other)', 'dir', 250 * 4095 - 3 * 2048, 2997, undefined, 2997],
    ])
  })

  it('a rollup under an owner pool, a heavy literal on a scan past the drill base: the view reads as before', async () => {
    const asked: string[] = []
    const src = drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))
    const spy: HitSource = { async hits(k, u) { asked.push(`${k}@${u}`); return src.hits(k, u) } }
    const env = envStatic(heavyStore(spy))
    const [onA, onB, pool] = await Promise.all([view(env, A, 'bk/fill', '0'), view(env, B, 'bk/fill', '0'), view(env, A, 'bk/fill', '0', { owner: 'unowned' })])
    expect([onA.tier, onB.tier.split('+')[0], pool.tier.split('+')[0], asked]).toEqual(['rollup', 'search', 'search', ['0@bk/fill', '0@bk/fill', '0@bk/fill']])
    const [rB, rPool] = await Promise.all([view(base, B, 'bk/fill', '0'), view(base, A, 'bk/fill', '0', { owner: 'unowned' })])
    expect([sansP1(onB), sansP1(pool)]).toEqual([sansP1(rB), sansP1(rPool)])
  })
})

describe('a heavy literal on a scan its drilldown doesn\'t cover (a run with no live `drill/` cuts the stack)', () => {
  /** The drill covering `A` only (B past it, as cw's scans past a merged run built without its `drill/`), the
   *  fleet root's catalog beside it; `noSearch`: B's search sidecars absent (cw: "no search index"). */
  const cutEnv = () => {
    const src = drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))
    const s = heavyStore(src)
    return envStatic({ ...s, catalog: new StaticCatalog(drillBlobs()) })
  }
  /** A search cut before it is exact (cw had no search sidecars at all: the same outcome, no exact search). */
  const CUT: SearchLimits = { ...SEARCH_LIMITS, triRgs: 1, namesRgs: 1, pathRgs: 1, rowsRgs: 1, keptRows: 1 }
  const outcome = async (p: Promise<View>) => {
    try {
      const v = await p
      return [v.tier.split('+')[0], v.matchCount ?? null, v.approximate ?? null]
    } catch (e) {
      if (e instanceof FilterRejected) return ['refused', e.reject.code]
      throw e
    }
  }

  it('below the root: an exact search answers; with none, refused `scan-not-indexed` — never the approximate "no matches" the base\'s thresholded read gives; the covered scan from the drill', async () => {
    const env = cutEnv()
    const exp = expected.views['0']['bk/fill']
    const searched = await view(env, B, 'bk/fill', '0')
    const walked = await view(base, B, 'bk/fill', '0', { searchLimits: CUT })
    expect([
      [searched.tier.split('+')[0], searched.partial ?? null, searched.approximate ?? null],
      await outcome(view(env, B, 'bk/fill', '0', { searchLimits: CUT })),
      await outcome(view(env, B, 'bk', '0', { searchLimits: CUT })),
      [walked.partial ?? walked.approximate ?? null],
      await outcome(view(env, A, 'bk/fill', '0')),
    ]).toEqual([
      ['search', null, null],
      ['refused', 'scan-not-indexed'],
      ['refused', 'scan-not-indexed'],
      [true],
      ['rollup', { n: 3000, b: total(exp[A]!)[0], o: total(exp[A]!)[1] }, null],
    ])
  })

  it('at the fleet root: the catalog\'s per-bucket cells (exact bytes and objects on the uncovered scan); under an owner, refused', async () => {
    const env = cutEnv()
    const v = await view(env, B, '', '0', { searchLimits: CUT })
    const [b, o] = total(expected.views['0'][''][B]!)
    expect([v.tier, v.tree.b, v.tree.o, v.rollup, v.approximate]).toEqual(['rollup', b, o, { children: 1, kept: 1, rows: null, bucketsOnly: true }, undefined])
    expect(await outcome(view(env, B, '', '0', { owner: 'unowned', searchLimits: CUT }))).toEqual(['refused', 'scan-not-indexed'])
  })

  it('a member the drill\'s live tiers don\'t know (its heavy source declines): refused, not walked', async () => {
    const src = drillSource(drillBlobs(undefined, { 'scans.json': { scans: [{ id: A }] } }))
    const none: HitSource = { hits: async () => null }
    const env = envStatic(heavyStore(none))
    expect([await outcome(view(env, B, 'bk/fill', '0', { searchLimits: CUT })), (await src.hits('nomatch', ''))]).toEqual([['refused', 'scan-not-indexed'], null])
  })
})

describe('the diff from the drilldown (`/api/diff`)', () => {
  it('a rollup: each kept child\'s two scans\' totals, the remainder\'s Δ in `(other)`', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of TERMS) for (const P of expected.paths.filter(p => p !== 'nope')) {
      const a = await drill.view(t, P)
      if (!('rollup' in a)) continue
      const d = await buildDiff(envStatic(), { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 10_000, query: q(t), threshold: 0.5 })
      const ea = expected.views[t][P][A]!, eb = expected.views[t][P][B]!
      const kept = keptOf(a)
      const named = kept.filter(c => ea[c] || eb[c]).sort()
      const ka = total(Object.fromEntries(named.map(c => [c, ea[c] ?? [0, 0]]))), kb = total(Object.fromEntries(named.map(c => [c, eb[c] ?? [0, 0]])))
      const ta = total(ea), tb = total(eb)
      const st = (x: [number, number] | undefined, y: [number, number] | undefined): DiffRow['s'] => !x ? 'added' : !y ? 'removed' : x[0] !== y[0] || x[1] !== y[1] ? 'changed' : 'unchanged'
      const rows: unknown[] = named.map(c => [c, st(ea[c], eb[c]), ea[c]?.[0] ?? 0, eb[c]?.[0] ?? 0, ea[c]?.[1] ?? 0, eb[c]?.[1] ?? 0])
      const ra: [number, number] = [ta[0] - ka[0], ta[1] - ka[1]], rb: [number, number] = [tb[0] - kb[0], tb[1] - kb[1]]
      if (ra[0] > 0 || rb[0] > 0) rows.push(['(other)', st(ra[0] > 0 ? ra : undefined, rb[0] > 0 ? rb : undefined), ra[0], rb[0], ra[0] > 0 ? ra[1] : 0, rb[0] > 0 ? rb[1] : 0])
      const sortRows = (rs: unknown[]) => (rs as [string][]).sort((x, y) => (x[0] < y[0] ? -1 : 1))
      got.push([t, P, d.total_a, d.total_b, d.objects_a, d.objects_b, d.lookups, sortRows(d.rows.filter(r => r.s !== 'unchanged').map(r => [r.p, r.s, r.a, r.b, r.oa, r.ob]))])
      want.push([t, P, ta[0], tb[0], ta[1], tb[1], 0, sortRows(rows.filter(r => (r as unknown[])[1] !== 'unchanged'))])
    }
    expect(got).toEqual(want)
    expect(got.length).toBe(13)
  })

  it('a roots answer: the same diff as the search sidecars\'', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of ['tomat', 'bin', 'x', 'om', 'ckpt-', 'safetensors']) for (const P of ['', 'bk', 'bk/data', 'zz']) {
      const a = await drill.view(t, P)
      if (a.source !== 'roots') continue
      for (const extra of [{}, { depth: 1 }, { summary: true }]) {
        const o = { from: A, to: B, path: P, w: W, h: H, minArea: MIN_AREA, atten: ATTEN, top: 200, query: q(t), ...extra }
        const [s, r] = await Promise.all([buildDiff(envStatic(), o), buildDiff(base, o)])
        got.push([t, P, extra, { ...s, tier: s.tier.replace(/^(drill|search)\+/, '+') }])
        want.push([t, P, extra, { ...r, tier: r.tier.replace(/^(drill|search)\+/, '+') }])
      }
    }
    expect(got).toEqual(want)
    expect(got.length).toBe(66)
  })
})

describe('the fleet root\'s root count (`matchCount.n` of a catalog view)', () => {
  it('a short literal\'s = its roots read whole (the count reads only the index\'s edges); a long one\'s = its alias entry', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of TERMS) {
      const a = await drill.view(t, '')
      if (a.source !== 'catalog') continue
      const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
      const lo: [string, string] = [a.c, ''], hi: [string, string] = [a.c + '\0', '']
      const roots = (await drill.state()).tiers[0].files[a.kind].roots
      const sel = await roots.select(lo, hi, io)
      const all = await roots.read(lo, hi, sel.groups!, io, () => 1)
      got.push([t, a.rollup.rows])
      want.push([t, all.length])
    }
    expect(got).toEqual(want)
    expect(got.map(x => (x as [string, number])[0])).toEqual(['tomat', 'f00', '0', '.', 'a', 'om', 't'])
  })

  it('the count reads only the data groups straddling the range\'s edges, each on its own (not the span between)', async () => {
    const roots = (await newDrill().state()).tiers[0].files.short.roots
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of ['0', '.']) {
      const lo: [string, string] = [t, ''], hi: [string, string] = [t + '\0', '']
      const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
      await roots.count(lo, hi, io)
      const all = (await roots.select(lo, hi, { ...io })).groups!
      const edges = [all[0], all[all.length - 1]].filter(e => e.qMin < t || e.qMax > t || e.qMax === t + '\0')
      got.push([t, io.groups, io.bytes])
      want.push([t, edges.length, edges.reduce((s, e) => s + e.length, 0)])
    }
    expect(got).toEqual(want)
  })
})

describe('no drilldown (`FILTER_STATIC_HEAVY` off): a heavy literal never reads as the approximate walk\'s "no matches"', () => {
  /** The static store without a heavy source: the suffix index (literals over `maxRows` suffix rows are heavy)
   *  and, for a heavy literal's fleet root, the base catalog — as `staticFilterStore` wires it. */
  const noDrill = (maxRows = 0): StaticFilterStore =>
    ({ source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows, heavy: null, catalog: new StaticCatalog(drillBlobs()) }), scans: async () => [...DATES], gen: 'fixture' })
  /** Every catalog member (the literals the drill answers from the catalog at the fleet root). */
  const MEMBERS = ['tomat', 'f00', '0', '.', 'a', 'om', 't']
  /** A view's refusal, else its tier ('not found': a path the scan lacks). */
  const outcome = async (p: Promise<View>) => p.then(v => v.tier, e => e instanceof FilterRejected ? e.reject : e instanceof NotFound ? 'not found' : Promise.reject(e))

  it('the fleet root: bucket tiles and the match count exactly the catalog\'s cells (`/api/name-summary`\'s numbers) = brute force', async () => {
    const catalog = new StaticCatalog(drillBlobs())
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of MEMBERS) for (const d of DATES) {
      const v = await view(envStatic(noDrill()), d, '', t, { threshold: 0.5 })
      got.push([t, d, v.tier, v.tree.b, v.tree.o, top(v), matchedRows(v), v.matchCount, v.matchesCapped, v.rollup, v.approximate, v.partial])
      const { member } = await catalog.lookup(t)
      const cells = Object.entries(catalogAnswer(member!, [d])[d]).map(([c, [b, o]]) => [c, Number(b), Number(o)] as [string, number, number])
      // The same cells as brute force from the objects.
      expect([t, d, Object.fromEntries(cells.map(([c, b, o]) => [c, [b, o]]))]).toEqual([t, d, expected.views[t][''][d]])
      const [b, o] = total(expected.views[t][''][d]!)
      const roots = cells.filter(([c]) => c.toLowerCase().includes(t))
      const buckets = new Set(member!.cells.map(c => c.bucket)).size
      want.push([t, d, 'rollup', b, o,
        cells.map(([c, cb, co]) => [c, cb, co, ...(c.toLowerCase().includes(t) ? ['m'] : [])]).sort((x, y) => (y[1] as number) - (x[1] as number)),
        roots.sort((x, y) => (x[0] < y[0] ? -1 : 1)),
        { n: roots.length, b, o }, true, { children: buckets, kept: buckets, rows: null, bucketsOnly: true }, undefined, undefined])
    }
    expect(got).toEqual(want)
    // Spelled out for one: `tomat` on B — three buckets, `tomato-bk` itself a match root.
    const v = await view(envStatic(noDrill()), B, '', 'tomat', { threshold: 0.5 })
    expect([top(v), v.matchCount, v.rollup]).toEqual([
      [['bk', 20127, 8], ['tomato-bk', 110, 2, 'm'], ['zz', 10, 1]],
      { n: 1, b: 20247, o: 11 },
      { children: 3, kept: 3, rows: null, bucketsOnly: true },
    ])
  })

  it('a one- or two-character literal no name holds: exactly zero at the root (no walk, no approximate flag)', async () => {
    const v = await view(envStatic(noDrill()), A, '', 'qq')
    expect([v.tier, v.tree.b, v.tree.o, v.matched, v.approximate]).toEqual(['none', 0, 0, [], undefined])
  })

  it('below the root: `term-too-common`, never the walk — at every heavy path, both scans', async () => {
    const drill = newDrill()
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of MEMBERS) for (const P of expected.paths.filter(p => p !== '' && p !== 'nope')) {
      const a = await drill.view(t, P)
      if (a.source === 'plain' || a.source === 'none') continue
      for (const d of DATES) {
        const missing = await outcome(view(base, d, P, t)) === 'not found'
        got.push([t, P, d, await outcome(view(envStatic(noDrill()), d, P, t))])
        want.push([t, P, d, missing ? 'not found' : { code: 'term-too-common', message: REJECT_MESSAGES['term-too-common'] }])
      }
    }
    expect(got).toEqual(want)
    expect([got.length, got.filter(g => (g as unknown[])[3] === 'not found').length]).toEqual([222, 9])
  })

  it('the root under an owner pool (the catalog knows no owners): `unsupported-scope`, not the walk', async () => {
    expect(await outcome(view(envStatic(noDrill()), A, '', '0', { owner: 'unowned' }))).toEqual({ code: 'unsupported-scope', message: REJECT_MESSAGES['unsupported-scope'] })
  })

  it('a view the literal matches is the plain view, as before', async () => {
    expect(await outcome(view(envStatic(noDrill()), A, 'tomato-bk', 'tomat'))).toBe((await view(base, A, 'tomato-bk', 'tomat')).tier)
  })

  it('light literals (within the suffix bound) are unaffected: the same views as the search sidecars\'', async () => {
    const got: unknown[] = []
    const want: unknown[] = []
    for (const t of ['tomat', 'ckpt', 'bin', 'ttl']) for (const P of ['', 'bk', 'bk/data']) for (const d of DATES) {
      const [s, r] = await Promise.all([view(envStatic(noDrill(1e9)), d, P, t), view(base, d, P, t)])
      got.push([t, P, d, s.tier.split('+')[0], sansP1({ ...s, tier: '' }), matchedRows(s)])
      want.push([t, P, d, r.matched?.length ? 'static' : 'none', sansP1({ ...r, tier: '' }), matchedRows(r)])
    }
    expect(got).toEqual(want)
  })

  it('with the drilldown (`FILTER_STATIC_HEAVY=1`), unchanged: the root from the catalog with its root count, rollups below', async () => {
    const at = async (P: string) => { const v = await view(envStatic(), A, P, '0', { threshold: 0.5 }); return [P, v.tier, v.matchCount, v.rollup] }
    expect(await Promise.all(['', 'bk', 'bk/fill'].map(at))).toEqual([
      ['', 'rollup', { n: 3000, b: 1023750, o: 3000 }, { children: 1, kept: 1, rows: 3000 }],
      ['bk', 'rollup', { n: 3000, b: 1023750, o: 3000 }, { children: 1, kept: 1, rows: 3000 }],
      ['bk/fill', 'rollup', { n: 3000, b: 1023750, o: 3000 }, { children: 3000, kept: 3, rows: 3000 }],
    ])
  })
})
