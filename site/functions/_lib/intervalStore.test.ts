import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { type Ask, openIndex, pathGens, readAsks, readRows, type Row, usMap, withPathStore } from './index'
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

/** `INDEX_R2` over the fixture dir, counting what is read. */
function r2(reads: string[]): R2Bucket {
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    const mod = 'node:fs' // a variable: the Workers typecheck has no Node types
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array }
    const file = fixture(`iv/${key}`)
    if (!fs.existsSync(file)) return null
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
  return { get } as unknown as R2Bucket
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
