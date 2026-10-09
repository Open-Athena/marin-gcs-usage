import { beforeAll, describe, expect, it, vi } from 'vitest'
import { scanTime } from '../../src/scanSlug'
import { AnchoredHits, AnchoredSource, parseKey, segmentMatches, termInPath, termKey } from './staticAnchors'
import { rollupAt } from './staticDrill'
import { type Found, SuffixHits, staticLiteral } from './staticFilter'
import { type Blobs, type Hit } from './staticNames'
import { tiers } from './staticRuns'
import { parseAst } from './querySyntax'
import { fixture, readJson } from './testStore'

// Anchored search (`staticAnchors.ts`) over `fixtures/static-anchors/` (`gen.py`): a base generation of four scans and
// two runs, built by `dt_cloud.static_anchors` (R = 3, K = 2; 4-row name groups, 5-row suffix groups, 4-row rollup
// groups, 3-entry index groups). Every key's view at every path on every scan of each stack (the base alone, the
// first run, both) — light, scoped roots and rollups, at three whole-range bounds — equals brute force from the
// versions (`expected.json`).

type Sums = Record<string, [number, number]>
type Expected = { dates: string[]; base: string[]; paths: string[]; keys: string[]; R: number; K: number; views: Record<string, Record<string, Record<string, Sums>>> }
let E: Expected
const files = new Map<string, ArrayBuffer>()
const LAST = 'manifests/2026-10-01.json', FIRST = 'manifests/2026-09-15.json'

function blobsOf(opts: { hide?: string[] } = {}): Blobs {
  const has = (k: string) => files.has(k) && !opts.hide?.includes(k)
  const bytes = (k: string) => { if (!has(k)) throw new Error(`no fixture ${k}`); return files.get(k)! }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
    async list(prefix) { return [...files.keys()].filter(k => k.startsWith(prefix) && has(k)).sort() },
  }
}

async function readTree(dir: string, rel = ''): Promise<void> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
  for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
    const k = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) await readTree(`${dir}/${e.name}`, k)
    else if (k !== 'expected.json' && k !== 'gen.py') { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); files.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  }
}

beforeAll(async () => {
  await readTree('static-anchors')
  E = await readJson<Expected>('static-anchors/expected.json')
})

const STACKS = {
  base: { hide: [FIRST, LAST], scans: () => E.base },
  run1: { hide: [LAST], scans: () => E.dates.slice(0, E.base.length + 1) },
  run2: { hide: [], scans: () => E.dates },
} as const

function source(stack: keyof typeof STACKS, maxRows?: number): AnchoredSource {
  const blobs = blobsOf({ hide: [...STACKS[stack].hide] })
  return new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows })
}

const n = (x: bigint) => Number(x)
function childSums(hits: Hit[], P: string, date: string): Sums {
  const D = scanTime(date), acc = new Map<string, [number, number]>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt)) continue
    const c = (P === '' ? h.path : h.path.slice(P.length + 1)).split('/')[0]
    const e = acc.get(c) ?? [0, 0]
    e[0] += n(h.size); e[1] += n(h.n)
    acc.set(c, e)
  }
  return Object.fromEntries([...acc].filter(([, v]) => v[0] || v[1]).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0))
}

/** Every (key, P, date) view of a stack against brute force: `[got, want]` lists and the sources seen. */
async function sweep(s: AnchoredSource, dates: string[], stackScans: string[]) {
  const got: unknown[] = [], want: unknown[] = []
  const seen: Record<string, number> = {}
  for (const key of E.keys) for (const P of E.paths) {
    const f: Found | null = termInPath(key, P) ? null : await s.hits(key, P)
    const kind = termInPath(key, P) ? 'plain' : !f ? 'declined' : f.rollup ? 'rollup' : String(f.io.from === 'anchors' ? 'roots' : 'light')
    seen[kind] = (seen[kind] ?? 0) + 1
    if (kind === 'plain') continue
    if (!f) { expect(parseKey(key).mode).toBe('start'); expect(s.why(key)).toBe('term-too-common'); continue }
    expect(f.scans).toEqual(stackScans)
    for (const d of dates) {
      const b = E.views[key]?.[P]?.[d] ?? {}
      if (f.rollup) {
        const { kids, rest } = rollupAt(f.rollup, d)
        const kept = Object.fromEntries(kids.map(([c, kb, ko]) => [c, [n(kb), n(ko)]]))
        const other = Object.entries(b).filter(([c]) => !(c in kept)).reduce((a, [, v]) => [a[0] + v[0], a[1] + v[1]], [0, 0])
        got.push([key, P, d, kept, [n(rest[0]), n(rest[1])]])
        want.push([key, P, d, Object.fromEntries(Object.keys(kept).map(c => [c, b[c] ?? [0, 0]])), other])
      } else {
        got.push([key, P, d, childSums(f.hits!, P, d)])
        want.push([key, P, d, b])
      }
    }
  }
  return { got, want, seen }
}

describe('anchored views equal brute force', () => {
  for (const stack of ['base', 'run1', 'run2'] as const) {
    for (const maxRows of [undefined, 8, 0]) {
      it(`${stack}, whole-range bound ${maxRows ?? 'MAX_ROWS'}`, async () => {
        const scans = STACKS[stack].scans()
        const { got, want, seen } = await sweep(source(stack, maxRows), scans, scans)
        expect(got).toEqual(want)
        if (maxRows === undefined) expect(Object.keys(seen).sort()).toEqual(['light', 'plain'])
        else expect(Math.min(seen.roots ?? 0, seen.rollup ?? 0, seen.declined ?? 0)).toBeGreaterThan(0)
      })
    }
  }
})

describe('the anchored stack', () => {
  it('drops a run without `anchors/meta.json` and every later one (its scans out of the answers)', async () => {
    const blobs = blobsOf({ hide: ['deltas/2026-09-15/anchors/meta.json'] })
    const s = new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows: 0 })
    const st = await s.state()
    expect([st.tiers.length, st.scans]).toEqual([1, E.base])
    // `q$` light reads stay on every scan (the suffix shards are every tier's); scoped ones cover the anchored tiers.
    const light = await new AnchoredSource(tiers(blobs).tiers, blobs).hits('.json/', '')
    expect(light!.scans).toEqual(E.dates)
    const scoped = await s.hits('.json/', '')
    expect(scoped!.scans).toEqual(E.base)
  })
  it('a generation without anchors declines `^q` and `^q$` (and `q$` past the light bound)', async () => {
    const blobs = blobsOf({ hide: ['anchors/meta.json'] })
    const s = new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows: 0 })
    expect(await Promise.all(['/tomat', '/config.json/', '.json/'].map(k => s.hits(k, '')))).toEqual([null, null, null])
  })
})

describe('mutations are caught', () => {
  it('the contains literal\'s parent test in place of the per-segment one', async () => {
    const spy = vi.spyOn(AnchoredHits.prototype, 'add').mockImplementation(function (this: AnchoredHits, cols) {
      const k = this.rowKey
      this.rows_read += cols.path.length
      for (let i = 0; i < cols.path.length; i++) {
        if (this.mode === 'start' ? !cols.s[i].startsWith(k) : cols.s[i] !== k) continue
        const p = cols.path[i], slash = p.lastIndexOf('/')
        if (cols.depth[i] < 1 || (slash >= 0 && p.slice(0, slash).toLowerCase().includes(this.text))) continue
        this.hits.push({ path: p, depth: cols.depth[i], usr: cols.usr[i], vf: Number(cols.vf[i]), vt: Number(cols.vt[i]), size: cols.size[i], n: cols.n_files[i] })
      }
    })
    try {
      const { got, want } = await sweep(source('run2'), E.dates, E.dates)
      expect(got).not.toEqual(want)
    } finally { spy.mockRestore() }
  })
})

describe('keys and segments', () => {
  it('`termKey` marks anchors with `/`, `parseKey` reads them back', () => {
    const terms = [{ text: 'ab', start: true, end: false }, { text: '.gz', start: false, end: true }, { text: 'x', start: true, end: true }, { text: 'tomat', start: false, end: false }]
    expect(terms.map(termKey)).toEqual(['/ab', '.gz/', '/x/', 'tomat'])
    expect(terms.map(termKey).map(parseKey)).toEqual([{ text: 'ab', mode: 'start' }, { text: '.gz', mode: 'end' }, { text: 'x', mode: 'exact' }, { text: 'tomat', mode: null }])
  })
  it('`termInPath`: some segment matches (`big-tomato` is no `^tomat`)', () => {
    const cases: [string, string, boolean][] = [
      ['/tomat', 'b1/big-tomato', false], ['/tomat', 'b1/tomato', true], ['/b1', 'b1', true], ['/b1', '', false],
      ['.json/', 'b/sub.json', true], ['.json/', 'b/a.jsonx', false], ['/config.json/', 'b/config.json', true], ['/config.json/', 'b/config.json.bak', false],
      ['/config.json/', 'b/CONFIG.JSON', true], ['tomat', 'b1/big-tomato', true],
    ]
    expect(cases.map(([k, p]) => termInPath(k, p))).toEqual(cases.map(c => c[2]))
    expect([segmentMatches('abc', 'ab', 'start'), segmentMatches('abc', 'bc', 'end'), segmentMatches('abc', 'abc', 'exact'), segmentMatches('abc', 'b', null)]).toEqual([true, true, true, true])
  })
  it('`staticLiteral`: an anchored literal\'s key, null under its minimum length or with `/`', () => {
    const lit = (q: string) => staticLiteral(parseAst(q) ?? undefined)
    expect(['^tomat', '.json$', '^config.json$', '^ab', '^a', 'gz$', '.gz$', '^x$', '"^a"', 'tomat'].map(lit))
      .toEqual(['/tomat', '.json/', '/config.json/', '/ab', null, null, '.gz/', '/x/', '^a', 'tomat'])
  })
  it('`SuffixHits` hands anchored keys to the anchored source, and nobody else', async () => {
    const blobs = blobsOf()
    const t = tiers(blobs)
    const anchored = new AnchoredSource(t.tiers, blobs)
    const s = new SuffixHits(t.names, { anchored })
    const got = await s.hits('/config.json/', 'b2')
    expect(childSums(got!.hits!, 'b2', E.dates[E.dates.length - 1])).toEqual(E.views['/config.json/'].b2[E.dates[E.dates.length - 1]])
    expect(await new SuffixHits(t.names).hits('/config.json/', 'b2')).toBe(null)
  })
})
