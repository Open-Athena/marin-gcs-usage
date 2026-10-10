import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { type Ask, IV_BROKEN_TTL, IV_STATE_TTL, ivRetry, openIndex, pathGens, readAsks, readRows, type Row, usMap, withPathStore } from './index'
import { sqliteD1 } from './testD1'
import { type D1Variant, fixture, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, type ViewNode } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The change-interval path store read as of a scan (`PATH_STORE=intervals`, specs/interval-store.md):
// `fixtures/iv/` is one generation over six synthetic scans (three v1 indexes, three v2 store
// generations; owner slices, paths that come and go, moving read days), 4 rows per group, built by
// `dt_cloud.interval_store` (`fixtures/iv/gen.py`). `expected.json` holds each view computed by the
// Python per-scan reference from that scan's own rows; the Worker's view over the store must equal it.

type Tiles = Record<string, Record<string, unknown>>
type Case = { date: string; path: string; w: number; h: number; depth: number | null; v: number; tiles: Tiles }

/** `INDEX_R2` over the fixture dir, counting what is read; `hide(key)`: an object that isn't there (a run's file not
 *  copied yet), to `get` and `list`, and with `head: false` the binding has no `head` (so only a read finds it). */
function r2(reads: string[], hide: (key: string) => boolean = () => false, { head = true }: { head?: boolean } = {}): R2Bucket {
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  const nodeFs = async () => {
    const mod = 'node:fs' // a variable: the Workers typecheck has no Node types
    return (await import(/* @vite-ignore */ mod)) as Fs
  }
  const there = async (key: string) => !hide(key) && (await nodeFs()).existsSync(fixture(`iv/${key}`))
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const fs = await nodeFs()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${dir}${f}`).filter(k => k.startsWith(prefix) && !hide(k) && fs.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    const fs = await nodeFs()
    const file = fixture(`iv/${key}`)
    if (!await there(key)) return null
    const buf = fs.readFileSync(file)
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    reads.push(key)
    const part = buf.slice(offset, offset + length)
    return {
      size: buf.byteLength,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
      json: async () => JSON.parse(new TextDecoder().decode(part)),
    }
  }
  return { get, list, ...(head ? { head: async (key: string) => (await there(key) ? { key } : null) } : {}) } as unknown as R2Bucket
}

/** A node without its children as canonical JSON (`interval_read.canon`: sorted keys, compact). */
function canon(n: ViewNode): string {
  const sorted = (v: unknown): unknown =>
    Array.isArray(v) ? v.map(sorted) : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sorted((v as Record<string, unknown>)[k])])) : v
  const { c: _c, ...rest } = n
  return JSON.stringify(sorted(rest))
}

/** A tree flattened as `interval_read.flatten`: keyed by names from the root, siblings by bytes then
 *  name, later duplicates `#2`…; owners by bytes then name (the Worker keeps row order on ties). */
function flatten(t: ViewNode): Tiles {
  const out: Tiles = {}
  const rec = (n: ViewNode, p: string) => {
    let k = p
    for (let i = 2; k in out; i++) k = `${p}#${i}`
    const { c, ...rest } = n
    const us = rest.us as [string, number][] | undefined
    out[k] = { ...rest, ...(us ? { us: [...us].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)) } : {}) }
    const kids = [...(c ?? [])].sort((a, b) => b.b - a.b || (a.n < b.n ? -1 : a.n > b.n ? 1 : 0) || (canon(a) < canon(b) ? -1 : canon(a) > canon(b) ? 1 : 0))
    for (const k2 of kids) rec(k2, `${p}/${k2.n}`)
  }
  rec(t, '')
  return out
}

let env: Env
let reads: string[]
let cases: Case[]

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db } = await sqliteD1('cw')
  reads = []
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: r2(reads) } as Env
  cases = await readJson<Case[]>('iv/expected.json')
})

describe('interval store', () => {
  it('opens a held date as of its scan, and nothing else', async () => {
    const h = await openIndex(env, '2026-08-03', 'bysize')
    expect([h.mode, h.version, h.variant, h.asOf, h.gen]).toEqual(['pq', 3, 'bysize', 1785715200, 'iv:g1'])
    await expect(openIndex(env, '2026-08-01', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-01")
    await expect(openIndex(env, '2026-08-03', 'bysize-user')).rejects.toThrow("index variant 'bysize-user' not synced for 2026-08-03")
  })

  it('opts a request in with `ps=iv` under `PATH_STORE=opt-in`, never for a secondary store', () => {
    const at = (q: string, mode = 'opt-in') => withPathStore({ request: new Request(`https://x/api/subtree?${q}`), env: { ...env, PATH_STORE: mode } }).env.PATH_STORE
    expect([at('date=d'), at('date=d&ps=iv'), at('date=d&ps=iv&store=meta'), at('date=d', 'intervals'), at('date=d&store=meta', 'intervals')])
      .toEqual([undefined, 'intervals', undefined, 'intervals', undefined])
  })

  it('keys a held date\'s answers to the interval generation', async () => {
    const off = { ...env, PATH_STORE: undefined }
    const [held, heldOff, other, otherOff] = await Promise.all([pathGens(env, ['2026-08-03']), pathGens(off, ['2026-08-03']), pathGens(env, ['2026-08-01']), pathGens(off, ['2026-08-01'])])
    expect([held === heldOff, other === otherOff]).toEqual([false, true])
  })

  it('keys a rewritten generation apart by its revision, reading the same objects', async () => {
    const rev = { ...env, INTERVAL_STORE_REV: '2' }
    const [h, g, g0] = await Promise.all([openIndex(rev, '2026-08-03', 'path'), pathGens(rev, ['2026-08-03']), pathGens(env, ['2026-08-03'])])
    const c = cases.find(x => x.date === '2026-08-03' && x.path === 'b2/e' && x.w === 8 && x.depth == null)!
    const v = await buildView(rev, { date: c.date, path: c.path, w: 8, h: 6, minArea: 12, atten: 2 })
    expect([h.gen, g === g0, flatten(v.tree)['']]).toEqual(['iv:g1@2', false, c.tiles['']])
  })

  it('decodes owner slices', () => {
    expect([usMap('', 5), usMap('alice', 5), usMap('[["alice",0],["bob",10]]', 10)]).toEqual([null, { alice: 5 }, { alice: 0, bob: 10 }])
  })

  it('serves every view of every scan as the per-scan reference draws it', async () => {
    expect(cases.length).toBe(84)
    for (const c of cases) {
      const v = await buildView(env, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ...(c.depth != null ? { maxDepth: c.depth } : {}) })
      const got = flatten(v.tree)
      // A v1 scan never knew its directories' children: the reader's `(other)` count is what its read saw.
      const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
      if (c.v === 1) for (const n of Object.values(got)) delete n.f
      expect({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: got }).toEqual({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: want })
      expect([v.index, ['bysize', 'path'].includes(v.tier)]).toEqual(['iv:g1', true])
    }
  })

  it('reads only the interval store, read days from the path versions themselves', async () => {
    expect([...new Set(reads)].sort()).toEqual([
      'interval-store/g1/scans.json',
      'interval-store/g1/served/bysize.groups.parquet',
      'interval-store/g1/served/bysize.parquet',
      'interval-store/g1/served/path.groups.parquet',
      'interval-store/g1/served/path.parquet',
    ])
  })

  it('a lookup bounded by another scan\'s version of the path finds exactly what an unbounded one does', async () => {
    // What a diff knows of a name it looks up (`Ask.vtLe` / `vfGe`): the other scan's row. A path's
    // versions never overlap, so the one live here closed by the time that row's opened, or opened once
    // it closed. Every row live at one scan, asked at every other where it isn't live. (This store's
    // segments each hold the versions closing at one scan, so a scan's own time test already selects
    // only groups that can answer and the bound prunes nothing more; on dyadic segments it skips the
    // open segment and the blocks outside the window — measured on gcs, specs/interval-store.md §6.2.)
    const DATES = [...new Set(cases.map(c => c.date))].sort()
    const live = new Map(await Promise.all(DATES.map(async d => [d, await readRows(await openIndex(env, d, 'path'), 1, 64, '', '\uffff')] as const)))
    const key = (r: { depth: number; path: string }) => `${r.depth}\0${r.path}`
    const rowsOf = (rs: Row[]) => rs.map(r => [r.depth, r.path, r.vf, r.vt, r.size, r.n_files]).sort((x, y) => (`${x[0]}${x[1]}${x[2]}` < `${y[0]}${y[1]}${y[2]}` ? -1 : 1))
    let asked = 0
    let found = 0
    const groups = { bounded: 0, plain: 0 }
    for (const here of DATES) {
      const h = await openIndex(env, here, 'path')
      const D = h.asOf!
      for (const there of DATES) {
        if (there === here) continue
        const asks: Ask[] = live.get(there)!.filter(r => !(r.vf! <= D && D < r.vt!))
          .map(r => ({ depth: r.depth, path: r.path, ...(D < r.vf! ? { vtLe: r.vf! } : { vfGe: r.vt! }) }))
        const want = new Set(asks.map(a => key(a as { depth: number; path: string })))
        const keep = (r: Row) => want.has(key(r))
        const [b, u] = await Promise.all([
          readAsks(h, asks, keep, { maxGroups: 1 << 20 }),
          readAsks(h, asks.map(a => ({ depth: a.depth, path: (a as { path: string }).path })), keep, { maxGroups: 1 << 20 }),
        ])
        expect({ here, there, rows: rowsOf(b.rows) }).toEqual({ here, there, rows: rowsOf(u.rows) })
        asked += asks.length
        found += b.rows.length
        groups.bounded += b.groups
        groups.plain += u.groups
      }
    }
    expect({ asked, found, groups }).toEqual({ asked: 580, found: 510, groups: { bounded: 155, plain: 155 } })
  })

  it('leaves a per-scan read of a date it holds as it was: one isolate serves both under `PATH_STORE=opt-in`', async () => {
    // A per-scan v1 generation of the store's first scan, with a coarse tier (the fine tier's rows, floor 1).
    const D = '2026-07-30'
    const { db, raw } = await sqliteD1('cw')
    const v1 = await readJson<Record<string, D1Variant>>('path-index-zstd.d1.json')
    const tier = { parquet: 'path-index-zstd.parquet', groups: 'path-index-zstd.groups.json' }
    seedGeneration(raw, { date: D, gen: 'p1', dir: `listing/${D}/index/p1`, variants: { ...v1, coarse20: { ...v1.path, schema: { ...v1.path.schema, floor_bytes: 1 } } }, files: { path: tier, coarse20: tier } })
    const per = { DB: db, ROOT_LABEL: 'all buckets', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
    const o = { date: D, path: '', w: 1280, h: 800, minArea: 12, atten: 2, threshold: 1 }
    const before = await buildView(per, o)
    // The store's read of the date finds its `path` sort a store sort, so no coarse tier answers it…
    const held = await buildView({ ...per, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g1', INDEX_R2: r2([]) } as Env, o)
    // …which says nothing about the per-scan generation's coarse tier.
    const after = await buildView(per, o)
    expect([before.index, before.tier, held.index, after.index, after.tier, after.tree]).toEqual(['d1', 'coarse20', 'iv:g1', 'd1', 'coarse20', before.tree])
  })

  it('diffs two scans from the store', async () => {
    const d = await buildDiff(env, { from: '2026-08-04', to: '2026-08-05', path: 'b2/e', w: 8, h: 6, minArea: 12, atten: 2, top: 100 })
    const a = cases.find(c => c.date === '2026-08-04' && c.path === 'b2/e' && c.w === 8 && c.depth == null)!
    const b = cases.find(c => c.date === '2026-08-05' && c.path === 'b2/e' && c.w === 8 && c.depth == null)!
    expect([d.total_a, d.total_b]).toEqual([a.tiles[''].b, b.tiles[''].b])
  })
})

// Base + per-scan runs (specs/interval-store.md §2.7, `interval_append`): `g3` and `g4` hold `g1`'s six scans as a base
// of four and a run per later scan — unmerged (two tiers) and merged into one by the binary counter — listed by the
// newest `manifests/<scan>.json` (`fixtures/iv/gen.py tiered`). Every view must be `g1`'s, and a broken run is cut out,
// never a failed request.
describe('interval store: base + runs', () => {
  const RUN_DATES = ['2026-08-04', '2026-08-05']
  // `rev`: a test's own isolate caches (state, footers, groups and broken runs are keyed by the generation's revision).
  const at = (gen: string, reads: string[] = [], hide?: (k: string) => boolean, o?: { head?: boolean; rev?: string }): Env =>
    ({ ...env, INTERVAL_STORE_GEN: gen, INDEX_R2: r2(reads, hide, o), ...(o?.rev ? { INTERVAL_STORE_REV: o.rev } : {}) }) as Env

  it('opens every scan with every run beside the base', async () => {
    const shape = async (gen: string, date: string) => {
      const h = await openIndex(at(gen), date, 'path')
      return [h.asOf, h.gen, (h.runs ?? []).map(r => [r.run, r.gen, r.asOf])]
    }
    expect(await Promise.all([shape('g3', '2026-08-03'), shape('g3', '2026-08-05'), shape('g4', '2026-08-04')])).toEqual([
      [1785715200, 'iv:g3', [['deltas/2026-08-04', 'iv:g3/deltas/2026-08-04', 1785715200], ['deltas/2026-08-05', 'iv:g3/deltas/2026-08-05', 1785715200]]],
      [1785888000, 'iv:g3', [['deltas/2026-08-04', 'iv:g3/deltas/2026-08-04', 1785888000], ['deltas/2026-08-05', 'iv:g3/deltas/2026-08-05', 1785888000]]],
      [1785801600, 'iv:g4', [['deltas/2026-08-04_2026-08-05', 'iv:g4/deltas/2026-08-04_2026-08-05', 1785801600]]],
    ])
  })

  for (const gen of ['g3', 'g4']) {
    it(`serves every view of every scan as g1 does (${gen}: runs ${gen === 'g3' ? 'unmerged' : 'merged'})`, async () => {
      const e = at(gen)
      for (const c of cases) {
        const v = await buildView(e, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2, ...(c.depth != null ? { maxDepth: c.depth } : {}) })
        const got = flatten(v.tree)
        const want = Object.fromEntries(Object.entries(c.tiles).map(([k, n]) => [k, n.f === null ? Object.fromEntries(Object.entries(n).filter(([f]) => f !== 'f')) : n]))
        if (c.v === 1) for (const n of Object.values(got)) delete n.f
        expect({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: got }).toEqual({ case: [c.date, c.path, c.w, c.h, c.depth], tiles: want })
        expect(v.index).toBe(`iv:${gen}`)
      }
    })

    it(`diffs every pair of scans as g1 does (${gen})`, async () => {
      const dates = [...new Set(cases.map(c => c.date))].sort()
      for (const from of dates) {
        for (const to of dates) {
          if (from === to) continue
          for (const path of ['', 'b1', 'b2/e']) {
            const o = { from, to, path, w: 8, h: 6, minArea: 12, atten: 2, top: 100 }
            const shape = (d: Awaited<ReturnType<typeof buildDiff>> | string) => typeof d === 'string' ? d : { rows: d.rows, totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
            const got = await buildDiff(at(gen), o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
            const want = await buildDiff(env, o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
            expect({ o, d: shape(got) }).toEqual({ o, d: shape(want) })
          }
        }
      }
    })
  }

  it('point lookups find each scan\'s rows, bounded or not, as g1 does', async () => {
    const dates = [...new Set(cases.map(c => c.date))].sort()
    const rows = (rs: Row[]) => rs.map(r => [r.depth, r.path, r.size, r.n_files, r.kind, r.last_read]).sort((x, y) => (`${x[0]}${x[1]}` < `${y[0]}${y[1]}` ? -1 : 1))
    for (const d of dates) {
      const [a, b] = await Promise.all([openIndex(env, d, 'path'), openIndex(at('g3'), d, 'path')])
      const asks: Ask[] = [{ depth: 1, under: '' }, { depth: 2, under: 'b1' }, { depth: 3, under: 'b2/e' }, { depth: 2, path: 'b2/e' }]
      const [x, y] = await Promise.all([readAsks(a, asks, () => true), readAsks(b, asks, () => true)])
      expect({ d, rows: rows(y.rows) }).toEqual({ d, rows: rows(x.rows) })
    }
  })

  it('reads the base and the runs, nothing else', async () => {
    const reads: string[] = []
    const e = at('g3', reads, undefined, { rev: 'reads' })
    for (const c of cases.filter(x => x.w === 30 && x.depth == null)) await buildView(e, { date: c.date, path: c.path, w: c.w, h: c.h, minArea: 12, atten: 2 })
    const files = (d: string) => ['bysize', 'path'].flatMap(x => [`${d}${x}.groups.parquet`, `${d}${x}.parquet`])
    expect([...new Set(reads)].sort()).toEqual([
      ...files('interval-store/g3/deltas/2026-08-04/served/'),
      ...files('interval-store/g3/deltas/2026-08-05/served/'),
      'interval-store/g3/manifests/2026-08-05.json',
      'interval-store/g3/scans.json',
      ...files('interval-store/g3/served/'),
    ].sort())
  })

  it('cuts the stack at a run whose files are missing: its scans leave the store, the ones before it answer as before', async () => {
    // g3's second run listed before its sorts reached R2: its footers (and the data, `head`) are not there.
    const hide = (k: string) => k.startsWith('interval-store/g3/deltas/2026-08-05/')
    const e = at('g3', [], hide, { rev: 'cut1' })
    await expect(openIndex(e, '2026-08-05', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-05")
    const h = await openIndex(e, '2026-08-04', 'path')
    expect((h.runs ?? []).map(r => r.run)).toEqual(['deltas/2026-08-04'])
    const c = cases.find(x => x.date === '2026-08-04' && x.path === '' && x.w === 30 && x.depth == null)!
    expect(flatten((await buildView(e, { date: c.date, path: '', w: 30, h: 30, minArea: 12, atten: 2 })).tree)).toEqual(c.tiles)
  })

  it('cuts a run a read finds broken, and the retry reads around it: never a failed request', async () => {
    // A run's data file gone with its footer still there, on a binding without `head`: only a read finds it.
    let gone = true
    const hide = (k: string) => gone && k === 'interval-store/g4/deltas/2026-08-04_2026-08-05/served/bysize.parquet'
    const at4 = (rev: string) => at('g4', [], hide, { head: false, rev })
    const o = { date: '2026-08-03', path: '', w: 30, h: 30, minArea: 12, atten: 2 }
    const want = (await buildView(env, o)).tree
    // As a request reads it (`ivRetry`): the first try finds the run broken and cuts it, the retry reads the base.
    expect((await ivRetry(() => buildView(at4('a'), o))).tree).toEqual(want)
    // The same read without the retry fails once, then the stack is cut: the run's scans read per-scan (none here:
    // "not synced"), the base's from the base alone.
    await expect(buildView(at4('b'), o)).rejects.toThrow('interval store run deltas/2026-08-04_2026-08-05 is broken')
    await expect(openIndex(at4('b'), '2026-08-05', 'path')).rejects.toThrow("index variant 'path' not synced for 2026-08-05")
    expect((await openIndex(at4('b'), '2026-08-03', 'path')).runs).toBeUndefined()
    expect((await buildView(at4('b'), o)).tree).toEqual(want)
    // Healed (the file is there again) once the cut expires.
    gone = false
    const t0 = Date.now()
    const now = vi.spyOn(Date, 'now').mockReturnValue(t0 + IV_BROKEN_TTL)
    try {
      expect(((await openIndex(at4('b'), '2026-08-05', 'path')).runs ?? []).map(r => r.run)).toEqual(['deltas/2026-08-04_2026-08-05'])
      expect((await buildView(at4('b'), o)).tree).toEqual(want)
    } finally {
      now.mockRestore()
    }
  })
})
