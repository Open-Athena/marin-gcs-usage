import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from '../_lib/testStore'
import { searchKey } from '../_lib/search'
import { type FilterRejectCode, REJECT_MESSAGES, rejectQuery } from '../_lib/indexedOnly'
import { drillSource, injectedStores, SuffixHits, type StaticFilterStore } from '../_lib/staticFilter'
import { type Blobs, StaticNames } from '../_lib/staticNames'
import { onRequestGet as subtree } from './subtree'
import { onRequestGet as diff } from './diff'
import { onRequestGet as series } from './series'
import { onRequestGet as caps } from './filter-caps'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `FILTER_INDEXED_ONLY=1` (`_lib/indexedOnly.ts`): the map's filter (subtree, diff, series) takes one literal
// substring of a name, unscoped, on a scan the static index covers — every other form, scope or scan is a 400
// `{ error, code }` before any read; unset, the same requests answer as before. Over the static-filter
// fixture's two scans (`fixtures/static-filter/`) and its drill (`fixtures/static-drill/`).

const A = '2026-10-04'
const B = '2026-10-05'
const dirOf = (date: string) => `cw-l2/${date}/index/g`
let base: Env
const filterFiles = new Map<string, ArrayBuffer>()
const drillFiles = new Map<string, ArrayBuffer>()

function blobsOf(files: Map<string, ArrayBuffer>, override: Record<string, unknown> = {}): Blobs {
  const bytes = (k: string) => { const b = files.get(k); if (!b) throw new Error(`no fixture ${k}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return key in override ? override[key] : JSON.parse(new TextDecoder().decode(bytes(key))) },
  }
}
async function readTree(dir: string, into: Map<string, ArrayBuffer>, rel = ''): Promise<void> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
  for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
    const k = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) await readTree(`${dir}/${e.name}`, into, k)
    else { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); into.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  }
}

/** The static store: the suffix index on `scans`, literals over 40 suffix rows from the drill, whose base
 *  generation is `drillScans`. */
function store(scans = [A, B], drillScans = [A, B]): StaticFilterStore {
  const heavy = drillSource(blobsOf(drillFiles, { 'scans.json': { scans: drillScans.map(id => ({ id })) } }))
  return { source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 40, heavy }), scans: async () => scans, gen: 'fixture' }
}
const envOf = (flag: boolean, s: StaticFilterStore = store()): Env => {
  const env = { ...base, ...(flag ? { FILTER_INDEXED_ONLY: '1' } : {}) } as Env
  injectedStores.set(env, s)
  return env
}

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  await readTree('static-drill', drillFiles)
  await readTree('static-filter/sx', filterFiles, 'sx')
  for (const k of ['shards.json', 'scans.json']) { const m = new Map<string, ArrayBuffer>(); await readTree('static-filter', m); filterFiles.set(k, m.get(k)!) }
  const { db, raw } = await sqliteD1('cw')
  for (const date of [A, B]) {
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
  base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
})

type Route = typeof subtree
const call = async (route: Route, qs: string, env: Env): Promise<[number, unknown]> => {
  const r = await route({ request: new Request(`http://localhost/api/x?${qs}`), env })
  const text = await r.text()
  return [r.status, r.headers.get('content-type')?.startsWith('application/json') && r.status === 400 ? JSON.parse(text) : r.status === 200 ? 'ok' : text]
}
const refusal = (code: FilterRejectCode) => [400, { error: REJECT_MESSAGES[code], code }]

/** Every rejected form, its code. */
const FORMS: [string, string, FilterRejectCode][] = [
  ['regex syntax', 'q=ckpt.*pt&qs=regex', 'unsupported-regex'],
  ['a /…/ regex', `q=${encodeURIComponent('/ckpt.*pt/')}`, 'unsupported-regex'],
  ['a glob', `q=${encodeURIComponent('tom*t')}`, 'unsupported-glob'],
  ['an exclusion', `q=${encodeURIComponent('tomat -bin')}`, 'unsupported-exclusion'],
  ['exclusions alone', `q=${encodeURIComponent('-ttl')}`, 'unsupported-exclusion'],
  ['two terms', `q=${encodeURIComponent('tomat bin')}`, 'unsupported-terms'],
  ['two short terms', `q=${encodeURIComponent('to at')}`, 'unsupported-terms'],
  ['alternatives', `q=${encodeURIComponent('tomat|ckpt')}`, 'unsupported-terms'],
  ['a `/`', `q=${encodeURIComponent('data/tomat')}`, 'unsupported-slash'],
  ['an owner pool', 'q=tomat&o=owned', 'unsupported-scope'],
  ['a user lens', 'q=tomat&lens=user:alice', 'unsupported-scope'],
  ['a class scope', 'q=tomat&cl=s', 'unsupported-scope'],
]

describe('rejectQuery: what an indexed-only deployment refuses', () => {
  it('one literal (any length, quoted spaces too) passes; every other form has its code', () => {
    const code = (q: string, qs?: string) => rejectQuery(q, qs)?.code ?? null
    expect([
      code('tomat'), code('TOMAT'), code('.'), code('ab'), code('"final ckpt"'), code(''), code('  '),
      code('ckpt.*pt', 'regex'), code('/ckpt/'), code('tom*t'), code('tomat -bin'), code('-ttl'), code('a b'), code('a|b'), code('data/tomat'),
    ]).toEqual([
      null, null, null, null, null, null, null,
      'unsupported-regex', 'unsupported-regex', 'unsupported-glob', 'unsupported-exclusion', 'unsupported-exclusion', 'unsupported-terms', 'unsupported-terms', 'unsupported-slash',
    ])
  })
})

describe('the map routes, flag set: each rejected form is a 400 with its code; unset, the same request answers', () => {
  for (const [what, qs, code] of FORMS) {
    it(what, async () => {
      expect(await Promise.all([
        call(subtree, `date=${A}&path=bk&${qs}`, envOf(true)),
        call(diff, `from=${A}&to=${B}&path=bk&${qs}`, envOf(true)),
        call(series, `path=bk&${qs}`, envOf(true)),
      ])).toEqual([refusal(code), refusal(code), refusal(code)])
      // Unset: served (a series of a non-static query wants its roots, `paths=`, as before; the fixture has no
      // ledger for a lens read).
      if (code === 'unsupported-scope' && qs.includes('lens=')) return
      if (what === 'two short terms') {
        expect(await call(subtree, `date=${A}&path=bk&${qs}`, envOf(false))).toEqual([400, 'bad query: type at least 3 characters (“to”)'])
        return
      }
      const unset = await Promise.all([
        call(subtree, `date=${A}&path=bk&${qs}`, envOf(false)),
        call(diff, `from=${A}&to=${B}&path=bk&${qs}`, envOf(false)),
      ])
      expect(unset).toEqual([[200, 'ok'], [200, 'ok']])
    })
  }
})

describe('the map routes, flag set: a heavy literal with no heavy source (`FILTER_STATIC_HEAVY` off)', () => {
  // the suffix index alone: a literal past its 40-row bound (or of one or two characters) has no source
  const light = (scans = [A, B]): StaticFilterStore => ({ source: new SuffixHits(new StaticNames(blobsOf(filterFiles)), { maxRows: 40 }), scans: async () => scans, gen: 'fixture' })
  it('says so in words', () => {
    expect(refusal('term-too-common')).toEqual([400, { error: 'This term matches too many files to search here; try a longer one.', code: 'term-too-common' }])
  })
  it('is `term-too-common` on an indexed scan — the term is the reason, not the scan', async () => {
    const env = envOf(true, light())
    expect(await Promise.all([
      call(subtree, `date=${A}&path=bk/fill&q=0`, env),
      call(subtree, `date=${B}&path=bk/fill&q=0`, env),
      call(diff, `from=${A}&to=${B}&path=bk/fill&q=0`, env),
      call(subtree, `date=${A}&path=bk&q=tomat`, env),
    ])).toEqual([refusal('term-too-common'), refusal('term-too-common'), refusal('term-too-common'), [200, 'ok']])
  })
  it('a scan outside the generation is still `scan-not-indexed`, whatever the term', async () => {
    const env = envOf(true, light([A]))
    expect(await Promise.all([
      call(subtree, `date=${B}&path=bk/fill&q=0`, env),
      call(subtree, `date=${B}&path=bk&q=tomat`, env),
    ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed')])
  })
})

describe('the map routes, flag set: a scan the static index doesn\'t cover', () => {
  it('a heavy literal past the drill base: subtree and diff refuse, the covered scan answers', async () => {
    const env = envOf(true, store([A, B], [A]))
    expect(await Promise.all([
      call(subtree, `date=${B}&path=bk/fill&q=0`, env),
      call(diff, `from=${A}&to=${B}&path=bk/fill&q=0`, env),
      call(subtree, `date=${A}&path=bk/fill&q=0`, env),
    ])).toEqual([refusal('scan-not-indexed'), refusal('scan-not-indexed'), [200, 'ok']])
  })

  it('a scan outside the generation (even a light literal); unset, it reads the path store as before', async () => {
    expect(await Promise.all([
      call(subtree, `date=${B}&path=bk&q=tomat`, envOf(true, store([A]))),
      call(subtree, `date=${B}&path=bk&q=tomat`, envOf(false, store([A]))),
    ])).toEqual([refusal('scan-not-indexed'), [200, 'ok']])
  })

  it('the series: covered scans\' points, the others named (`unindexed`), never read per scan', async () => {
    const r = await series({ request: new Request('http://localhost/api/series?path=bk/fill&q=0'), env: envOf(true, store([A, B], [A])) })
    const j = await r.json() as { points: { date: string }[]; unindexed?: string[] }
    expect([r.status, j.points.map(p => p.date), j.unindexed]).toEqual([200, [A], [B]])
  })

  it('a literal the view root holds is the plain view (nothing to search), on any scan', async () => {
    expect(await call(subtree, `date=${B}&path=bk/data/tomato&q=tomat`, envOf(true, store([A])))).toEqual([200, 'ok'])
  })

  it('a supported literal answers byte for byte as unset', async () => {
    const [on, off] = await Promise.all([true, false].map(async f => (await subtree({ request: new Request(`http://localhost/api/x?date=${A}&path=bk&q=tomat`), env: envOf(f) })).text()))
    expect(on).toEqual(off)
  })
})

describe('/api/filter-caps', () => {
  it('names the flag', async () => {
    const got = await Promise.all([envOf(true), envOf(false)].map(async env => (await caps({ request: new Request('http://localhost/api/filter-caps'), env } as never)).json()))
    expect(got).toEqual([{ indexedOnly: true }, { indexedOnly: false }])
  })
})
