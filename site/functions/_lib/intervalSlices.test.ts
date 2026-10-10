import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { openIndex, slices } from './index'
import type { OwnerScope } from './scope'
import { LEDGER_KIND, sqliteD1 } from './testD1'
import { type D1Variant, fixture, GETS, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, readRootAgg } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// Owner-scoped reads from the interval store's slice sorts (specs/interval-store.md §2.1): `fixtures/iv/
// interval-store/g2` (`fixtures/iv/gen.py g2`) is one generation over three scans of the per-scan store
// fixtures — `v2-slices` (multi-owner slices), `v2-lens`, `v2-slices` again. Every lens view, owner pool,
// root total and lens diff it serves must equal the per-scan reader's over the same per-scan files, and
// must read nothing per-scan.

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']
const base = { w: 1280, h: 800, minArea: 12, atten: 1 }
let perScan: Env
let iv: Env
const r2Reads: string[] = []

/** `INDEX_R2` over `fixtures/iv/`. */
function r2(): R2Bucket {
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    const mod = 'node:fs'
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array }
    const file = fixture(`iv/${key}`)
    if (!fs.existsSync(file)) return null
    r2Reads.push(key)
    const buf = fs.readFileSync(file)
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  return { get } as unknown as R2Bucket
}

const files = (dir: string, sorts: Record<string, string>) =>
  Object.fromEntries(Object.entries(sorts).map(([v, stem]) => [v, { parquet: `${dir}/${stem}.parquet`, groups: `${dir}/${stem}.groups.json` }]))

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  const slicesD1 = await readJson<Record<string, D1Variant>>('v2-slices/d1.json')
  const lensD1 = await readJson<Record<string, D1Variant>>('v2-lens/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: slicesD1, files: files('v2-slices', { path: 'path-index', bysize: 'path-index-bysize' }) })
  // The gcs ledger's tables, empty: owners are the scans' own attribution.
  raw.exec(`
    CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, ts INTEGER NOT NULL, scan TEXT NOT NULL, pattern TEXT NOT NULL, set_owner INTEGER NOT NULL DEFAULT 0, owner TEXT);
    CREATE TABLE owner_prefixes (action_id INTEGER NOT NULL REFERENCES actions (id), prefix TEXT NOT NULL, owner TEXT, ts INTEGER NOT NULL, tombstoned TEXT, PRIMARY KEY (prefix, action_id));
    ${LEDGER_KIND}
    CREATE TABLE owner_totals (scan TEXT NOT NULL, head INTEGER NOT NULL, body TEXT NOT NULL, claims TEXT NOT NULL, computed_ts INTEGER, ms INTEGER, PRIMARY KEY (scan, head));
  `)
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: lensD1, files: files('v2-lens', { path: 'path-index', bysize: 'path-index-bysize', 'bysize-user': 'path-index-bysize-by-user' }) })
  perScan = { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
  iv = { ...perScan, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g2', INDEX_R2: r2() } as Env
})

/** What a read did per-scan: the store's `listing/` keys it fetched. */
async function perScanGets<T>(f: () => Promise<T>): Promise<[T, string[]]> {
  const n = GETS.length
  const v = await f()
  return [v, [...new Set(GETS.slice(n).map(g => g.key))]]
}

const PATHS = ['', 'bk', 'bk/m', 'bk/m/deep', 'bk/nest', 'bk/flat']

/** The store's kind for a path that is both an object and a prefix (`bk/m/twin`: alice's 50 KiB object
 *  and 600 KiB dir; the fixture's only node named `twin`) is `dir`, its rows folded into one slice; the
 *  per-scan reader takes the kind of whichever row it merged last (`file` here). Every other field of
 *  the tile is the same. */
function storeKinds<T>(v: T): T {
  type N = { n?: string; k?: string; c?: N[] }
  const fix = (n: N): N => ({ ...n, ...(n.n === 'twin' ? { k: 'dir' } : {}), ...(n.c ? { c: n.c.map(fix) } : {}) })
  return (v && typeof v === 'object' && 'n' in (v as object) ? fix(v as N) : v) as T
}
const SCOPES: { lens?: { key: string }; owner?: OwnerScope }[] = [
  { lens: { key: 'alice' } }, { lens: { key: 'bob' } }, { lens: { key: 'carol' } },
  { owner: 'owned' }, { owner: 'unowned' }, { owner: { not: ['alice'] } },
]

describe('interval store: owner slices', () => {
  it('opens the slice sorts for a sliced read of a held date, and refuses its v1 `user` sort', async () => {
    const h = await openIndex(slices(iv), S1, 'bysize-user')
    expect([h.mode, h.variant, h.version, h.asOf, h.gen, h.columns?.includes('usr')]).toEqual(['pq', 'bysize-user', 3, 1790726820, 'iv:g2:s', true])
    await expect(openIndex(slices(iv), S1, 'user')).rejects.toThrow(`index variant 'user' not synced for ${S1}`)
  })

  it('serves every lens view and owner pool of every scan as the per-scan reader draws it, reading nothing per-scan', async () => {
    let drawn = 0
    for (const date of [S1, S2, S3]) {
      for (const path of PATHS) {
        for (const scope of SCOPES) {
          const o = { ...base, date, path, ...scope }
          const want = await buildView(perScan, o).catch(e => String(e))
          const [got, gets] = await perScanGets(() => buildView(iv, o).catch(e => String(e)))
          const tree = (v: typeof want) => (typeof v === 'string' ? v : v.tree)
          expect({ o, tree: tree(got), gets }).toEqual({ o, tree: storeKinds(tree(want)), gets: [] })
          if (typeof got !== 'string' && typeof want !== 'string') {
            // `none`: nothing in scope (an owner with no bytes there), drawn without a read.
            expect([got.index, want.index === 'none']).toEqual([want.index === 'none' ? 'none' : 'iv:g2', want.index === 'none'])
            drawn += got.index === 'none' ? 0 : 1
          }
        }
      }
    }
    expect(drawn).toBe(49)
  })

  it('totals a root by owner as the per-scan reader does', async () => {
    for (const date of [S1, S2, S3]) {
      for (const path of ['', 'bk', 'bk/m', 'bk/nest']) {
        for (const scope of SCOPES) {
          const o = { date, path, ...scope }
          expect({ o, agg: await readRootAgg(iv, o) }).toEqual({ o, agg: await readRootAgg(perScan, o) })
        }
      }
    }
  })

  it('diffs a lens and a pool across scans as the per-scan reader does', async () => {
    for (const [from, to] of [[S1, S2], [S2, S3], [S1, S3]]) {
      for (const scope of SCOPES) {
        const o = { ...base, from, to, path: '', top: 500, ...scope }
        // A lens with nothing on either side is a 404 from both.
        const shape = (d: Awaited<ReturnType<typeof buildDiff>> | string) => typeof d === 'string' ? d
          : { rows: d.rows.map(r => r.p.split('/').pop() === 'twin' ? { ...r, k: 'dir' } : r), totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
        // The store's read first: a per-scan diff that throws on one side leaves the other side's reads in flight.
        const [got, gets] = await perScanGets(() => buildDiff(iv, o).catch(e => `${(e as Error).name}: ${(e as Error).message}`))
        const want = await buildDiff(perScan, o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
        expect({ o, got: shape(got), gets }).toEqual({ o, got: shape(want), gets: [] })
      }
    }
  })

  it('reads the slice sorts, never the folded ones, for owner-scoped reads', () => {
    expect([...new Set(r2Reads.filter(k => k.includes('/served/')))].sort()).toEqual([
      'interval-store/g2/served/slices-bysize-user.groups.parquet',
      'interval-store/g2/served/slices-bysize-user.parquet',
      'interval-store/g2/served/slices-bytotal.groups.parquet',
      'interval-store/g2/served/slices-bytotal.parquet',
      'interval-store/g2/served/slices.groups.parquet',
      'interval-store/g2/served/slices.parquet',
    ])
  })
})
