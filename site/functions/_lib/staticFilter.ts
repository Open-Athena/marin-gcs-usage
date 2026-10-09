/** The map's name filter from the static name index on R2 (specs/architecture/static-name-search.md): a
 *  single-literal `?f=` query's match roots, exact on every scan of the generation, without walking the
 *  per-scan path store for them (`FILTER_STATIC=1` with the `INDEX_R2` binding).
 *
 *  Why it is exact: the filter matches a path when its lowercase full path (`bucket/dir/…`) contains the
 *  literal `q`. A literal without `/` lies inside one segment, so a path matches iff some segment contains
 *  `q`, and the outermost matching paths under a view root `P` that `P` itself does not match (the match
 *  roots, `filter.ts` `matchRoots`) are exactly the paths under `P` whose lowercase name contains `q` and
 *  whose lowercase parent does not — the static index's first hits, buckets (depth 1, parent `''`)
 *  included. Each first hit is one version of one owner slice `(path, usr)` with its `size`/`n_files`, so a
 *  root's bytes, objects and per-owner split on scan `D` are the sum of its slices live on `D`.
 *
 *  Which queries: `staticLiteral` — one positive substring matcher without `/`, no other term, glob, regex
 *  or exclusion. Which literals: those of ≥ 3 characters whose suffix range the shard group index bounds by
 *  `maxRows` (V = 100K plus two row groups of slack, so every non-member and the lightest members) are read
 *  whole once per isolate (and once per colo through the Cache API); heavier ones, and every one- or
 *  two-character literal, ask the `heavy` source — the drilldown (`staticDrill.ts`, `FILTER_STATIC_HEAVY=1`):
 *  the match roots under the view path when they are few, else the path's per-child rollup — and otherwise
 *  decline, so the caller keeps today's read.
 *
 *  Dates: an answer carries the scans it covers (`Found.scans`; absent = every scan of the store). The light
 *  index spans the base generation and its runs (one per scan); the drilldown only the base generation's scans, so a
 *  heavy literal on a newer scan declines (`covers`) and that view reads as before. */
import type { QueryAst } from './queryAst.js'
import { shared } from './shared.js'
import { StaticCatalog } from './staticCatalog.js'
import { Drill, DRILL_DIR, DrillSource, type Rollup } from './staticDrill.js'
import { type Blobs, cacheIndexes, type FirstHits, type Hit, type Io, r2Blobs, scanAt, staticGen, staticPrefix } from './staticNames.js'
import { tiers } from './staticRuns.js'
import { type FilterReject, reject } from './indexedOnly.js'

export type { Hit } from './staticNames.js'
export { type Rollup, rollupAt, rollupTotal } from './staticDrill.js'

/** The literal a query is, when the static index can answer it exactly (lowercase), else null. */
export function staticLiteral(ast: QueryAst | undefined): string | null {
  if (!ast || ast.neg.length || ast.alts.length !== 1 || ast.alts[0].length !== 1) return null
  const m = ast.alts[0][0]
  if (m.kind !== 'sub' || m.text.includes('/') || !m.text) return null
  return m.text
}

/** One or two characters: never a suffix-range read (the shards hold suffixes of ≥ 3); the heavy source's. */
export const shortLiteral = (key: string): boolean => [...key].length <= 2

/** What a source found for a literal under a path, any date: its match roots (`hits`: first hits, each a
 *  version of an owner slice), or — a heavy literal under a heavy directory — the path's per-child totals
 *  (`rollup`: kept children by name plus an exact remainder, no owners). `scans`: the scans the answer is
 *  exact on (absent: every scan of the store). */
export type Found =
  | { hits: Hit[]; rollup?: undefined; io: Record<string, unknown>; scans?: string[] }
  | { rollup: Rollup; hits?: undefined; io: Record<string, unknown>; scans?: string[] }

/** Whether `found` answers every one of `dates`: the explicit rule that keeps a heavy literal off the scans
 *  its drilldown does not cover (the runs past the base generation), which then read as before. */
export const covers = (found: Found, dates: string[]): boolean => !found.scans || dates.every(d => found.scans!.includes(d))

/** A literal's match roots under a path, any date: what the filter needs from an index. `null` = this
 *  source can't answer the literal (the caller falls back). `under` = `''` (everything) or a path, whose
 *  strict descendants are wanted. */
export interface HitSource {
  hits(key: string, under: string): Promise<Found | null>
}

/** The generation's scans (ids). */
export interface StaticFilterStore { source: HitSource; scans: () => Promise<string[]>; gen: string }

/** A suffix range read whole above this many rows (row-group granular) is declined: V = 100K (the
 *  catalog's membership bound, so every non-member fits) plus two 8K-row groups of slack. */
export const MAX_ROWS = 100_000 + 2 * 8192

/** Hits held per isolate, across literals (each ~150 B): the oldest literal is dropped past this. */
const HELD_HITS = 400_000

const under = (p: string, root: string): boolean => root === '' || p.startsWith(root + '/')

/** What `SuffixHits` reads: a literal's first hits, refused (null) above `maxRows`. `version` names the index's
 *  current state (the runs' manifest, by its newest scan, `staticRuns.ts`): the hit lists span every scan, so they are
 *  held and cached per version. A single generation is `StaticNames` (no `version`: 'base'). */
export interface HitReader {
  read(key: string, maxRows?: number): Promise<{ io: Io; fold: FirstHits | null }>
  version?(): Promise<string>
}

/** The light source: the literal's suffix range from the shards, folded to its first hits once (per
 *  isolate, and per colo through `cache`), then cut to `under`. Literals over `maxRows` go to `heavy`. */
export class SuffixHits implements HitSource {
  private held = new Map<string, Promise<{ hits: Hit[]; io: Record<string, unknown> } | null>>()
  private cut = new WeakMap<Hit[], Map<string, Hit[]>>()
  constructor(
    readonly names: HitReader,
    readonly opts: { maxRows?: number; heavy?: HitSource | null; cache?: HitCache | null; waitMs?: number } = {},
  ) {}

  /** Every first hit of `key` (null: over `maxRows`, the heavy source's business). */
  async all(key: string): Promise<{ hits: Hit[]; io: Record<string, unknown> } | null> {
    const version = (await this.names.version?.()) ?? 'base'
    // The base generation's entries keep their keys; a day's runs version theirs.
    const vkey = version === 'base' ? key : `${key}@${version}`
    const p = shared(this.held, vkey, async () => {
      const cached = await this.opts.cache?.get(vkey)
      if (cached) return { hits: cached, io: { from: 'cache', version } }
      const { io, fold } = await this.names.read(key, this.opts.maxRows ?? MAX_ROWS)
      if (!fold) return null
      await this.opts.cache?.put(vkey, fold.hits)
      return { hits: fold.hits, io: { from: 'shards', version, shard: io.shard, groups: io.groups, bytes: io.bytes, rows_read: io.rows_read, tiers: io.tiers ?? 1, ms: io.ms } }
    }, this.opts.waitMs ?? 20_000)
    // Bound what the isolate holds: drop the oldest literals past `HELD_HITS` hits.
    void p.then(() => this.trim(), () => {})
    return p
  }

  private async trim(): Promise<void> {
    let n = 0
    const keys = [...this.held.keys()].reverse()
    for (const k of keys) {
      const got = await this.held.get(k)?.catch(() => null)
      n += got?.hits.length ?? 0
      if (n > HELD_HITS && k !== keys[0]) this.held.delete(k)
    }
  }

  async hits(key: string, root: string): Promise<Found | null> {
    if (shortLiteral(key)) return this.opts.heavy ? this.opts.heavy.hits(key, root) : null
    const got = await this.all(key)
    if (!got) return this.opts.heavy ? this.opts.heavy.hits(key, root) : null
    if (root === '') return { hits: got.hits, io: got.io }
    // One array per (literal, root) while the literal is held: the view's per-isolate phase 1 is keyed by it.
    let byRoot = this.cut.get(got.hits)
    if (!byRoot) this.cut.set(got.hits, (byRoot = new Map()))
    let hits = byRoot.get(root)
    if (!hits) {
      byRoot.set(root, (hits = got.hits.filter(h => under(h.path, root))))
      if (byRoot.size > 32) byRoot.delete(byRoot.keys().next().value!)
    }
    return { hits, io: got.io }
  }
}

/** A literal's first hits across isolates (the colo's Cache API). */
export interface HitCache { get(key: string): Promise<Hit[] | null>; put(key: string, hits: Hit[]): Promise<void> }

/** Hits as columns of JSON (sizes as decimal strings: a bucket's bytes pass 2^53). */
export function cacheHits(cache: Cache, prefix: string, version = 'hits-v1'): HitCache {
  const url = (key: string) => `https://static-filter.invalid/${prefix}/${version}/${encodeURIComponent(key)}.json`
  return {
    async get(key) {
      const r = await cache.match(url(key))
      if (!r) return null
      const c = await r.json() as { path: string[]; depth: number[]; usr: string[]; vf: number[]; vt: number[]; size: string[]; n: string[] }
      return c.path.map((path, i) => ({ path, depth: c.depth[i], usr: c.usr[i], vf: c.vf[i], vt: c.vt[i], size: BigInt(c.size[i]), n: BigInt(c.n[i]) }))
    },
    async put(key, hits) {
      const c = { path: hits.map(h => h.path), depth: hits.map(h => h.depth), usr: hits.map(h => h.usr), vf: hits.map(h => h.vf), vt: hits.map(h => h.vt), size: hits.map(h => String(h.size)), n: hits.map(h => String(h.n)) }
      await cache.put(url(key), new Response(JSON.stringify(c), { headers: { 'content-type': 'application/json', 'cache-control': 'public, max-age=604800' } }))
    },
  }
}

export type StaticFilterEnv = { FILTER_STATIC?: string; FILTER_STATIC_HEAVY?: string; FILTER_INDEXED_ONLY?: string; INDEX_R2?: R2Bucket; STATIC_GEN?: string }

/** The drilldown over a bucket's generation (`drill/`, the base catalog for the fleet root, the base scans); `cached`:
 *  its indexes in `cache` under `prefix` (the generation's `staticPrefix`). */
export function drillSource(blobs: Blobs, cached?: { cache: Cache; prefix: string }): DrillSource {
  const { cache, prefix } = cached ?? {}
  const drillBlobs: Blobs = {
    range: (k, o, l) => blobs.range(`${DRILL_DIR}/${k}`, o, l),
    suffix: (k, n) => blobs.suffix(`${DRILL_DIR}/${k}`, n),
    json: k => blobs.json(`${DRILL_DIR}/${k}`),
  }
  const pre = `${prefix}/${DRILL_DIR}`
  const catalog = new StaticCatalog(blobs, cache ? cacheIndexes(cache, prefix!, 'catalog-v1') : undefined)
  const drill = new Drill(drillBlobs, catalog, cache ? { top: cacheIndexes(cache, pre, 'top-v1'), aliases: cacheIndexes(cache, pre, 'aliases-v2') } : undefined)
  return new DrillSource(drill, scanList(blobs), cache ? cacheHits(cache, pre, 'roots-v1') : undefined)
}

/** A test's store for an env object (in place of the R2 binding's). */
export const injectedStores = new WeakMap<object, StaticFilterStore>()

let held: { r2: R2Bucket; gen: string; store: StaticFilterStore } | undefined
/** The isolate's static filter store over the bound bucket; null when the static filter is off. A test
 *  injects its own (`injectedStores`). */
export function staticFilterStore(env: StaticFilterEnv): StaticFilterStore | null {
  const injected = injectedStores.get(env)
  if (injected) return injected
  if (env.FILTER_STATIC !== '1' || !env.INDEX_R2) return null
  const gen = staticGen(env), pre = staticPrefix(gen)
  if (held?.r2 !== env.INDEX_R2 || held.gen !== gen) {
    // The base generation plus its runs (`staticRuns.ts`): each tier's group indexes cached under its own prefix.
    const t = tiers(r2Blobs(env.INDEX_R2, pre), { indexCache: dir => cacheIndexes(caches.default, dir ? `${pre}/${dir}` : pre) })
    // Heavy literals (`FILTER_STATIC_HEAVY=1`): the drilldown over the base generation.
    const heavy = env.FILTER_STATIC_HEAVY === '1' ? drillSource(r2Blobs(env.INDEX_R2, pre), { cache: caches.default, prefix: pre }) : null
    held = { r2: env.INDEX_R2, gen, store: { source: new SuffixHits(t.names, { cache: cacheHits(caches.default, pre), heavy }), scans: t.scans, gen: `${gen}${heavy ? '+drill' : ''}` } }
  }
  return held.store
}

/** `scans.json`'s ids, sorted (held once loaded). */
export function scanList(blobs: Blobs): () => Promise<string[]> {
  let ids: Promise<string[]> | undefined
  return () => {
    ids ??= blobs.json<{ scans: { id: string }[] }>('scans.json').then(d => d.scans.map(s => s.id).sort())
    ids.catch(() => { ids = undefined })
    return ids
  }
}

/** Whether `date` can be answered statically for `ast`: the literal, else null. */
export async function staticKey(s: StaticFilterStore | null, ast: QueryAst | undefined, dates: string[]): Promise<string | null> {
  const key = staticLiteral(ast)
  if (!s || !key) return null
  const have = await s.scans()
  return dates.every(d => have.includes(d)) ? key : null
}

/** The filter's bytes and objects under the hits' root on `date` (Σ live first hits), with `keep` an
 *  owner test on the slice's `usr` (null = unowned). */
export function liveTotal(hits: Hit[], date: string, keep: (usr: string | null) => boolean = () => true): { b: number; o: number; roots: number } {
  const D = scanAt(date)
  let b = 0n, o = 0n
  const roots = new Set<string>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt) || !keep(h.usr === '' ? null : h.usr)) continue
    b += h.size; o += h.n; roots.add(h.path)
  }
  return { b: Number(b), o: Number(o), roots: roots.size }
}

/** Bumped when a static response's shape changes (2: roots folded under the pixel budget, capped lists;
 *  3: heavy literals from the drilldown, rollup views; 4: bounded phase 2 and the tile budget; 5: a 1–2
 *  character literal's fleet-root `matchCount.n` counted from the roots index, not 0). */
const RESPONSE_V = 5

/** The cache keys' static marker: the generation when the static filter would answer this query's literal
 *  (so a response never outlives a switch of backend or generation), else ''. */
export function staticTag(env: StaticFilterEnv, query: { ast?: QueryAst } | undefined): string {
  return staticLiteral(query?.ast) && staticFilterStore(env) ? `${staticFilterStore(env)!.gen}.${RESPONSE_V}` : ''
}

/** An indexed-only deployment's coverage test (`indexedOnly.ts`): `ast` (one literal, `rejectAst` passed) is
 *  answered statically under `path` on every one of `dates`, else `scan-not-indexed` — a scan outside the
 *  generation, or a heavy literal past the drill base. A view root the literal matches is the plain view
 *  (nothing to search). The answer is held per isolate, so the view's own read reuses it. */
export async function indexedGate(env: StaticFilterEnv, ast: QueryAst | undefined, path: string, dates: string[]): Promise<FilterReject | null> {
  const key = staticLiteral(ast)
  if (!key) return reject('unsupported-terms')
  if (path.toLowerCase().includes(key)) return null
  const s = staticFilterStore(env)
  if (!s || !await staticKey(s, ast, dates)) return reject('scan-not-indexed')
  const found = await s.source.hits(key, path)
  return found && covers(found, dates) ? null : reject('scan-not-indexed')
}
