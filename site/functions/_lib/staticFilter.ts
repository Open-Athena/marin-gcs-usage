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
 *  Which queries: `staticLiteral` — one positive substring matcher of ≥ 3 characters, no `/`, no other
 *  term, glob, regex or exclusion. Which literals: those whose suffix range the shard group index bounds
 *  by `maxRows` (V = 100K plus two row groups of slack, so every non-member and the lightest members) are
 *  read whole once per isolate (and once per colo through the Cache API); heavier ones ask the
 *  `heavy` source (`HitSource`, the forthcoming per-member "roots by path" files) and otherwise decline,
 *  so the caller keeps today's read. */
import type { Row } from './index.js'
import type { QueryAst } from './queryAst.js'
import { shared } from './shared.js'
import { type Blobs, cacheIndexes, type Hit, r2Blobs, scanMs, STATIC_GEN, STATIC_PREFIX, StaticNames } from './staticNames.js'

export type { Hit } from './staticNames.js'

/** The literal a query is, when the static index can answer it exactly (lowercase), else null. */
export function staticLiteral(ast: QueryAst | undefined): string | null {
  if (!ast || ast.neg.length || ast.alts.length !== 1 || ast.alts[0].length !== 1) return null
  const m = ast.alts[0][0]
  if (m.kind !== 'sub' || m.text.includes('/') || [...m.text].length < 3) return null
  return m.text
}

/** First hits of a literal under a path, any date: what the filter needs from an index. `null` = this
 *  source can't answer the literal (the caller falls back). `under` = `''` (everything) or a path, whose
 *  strict descendants are wanted. */
export interface HitSource {
  hits(key: string, under: string): Promise<{ hits: Hit[]; io: Record<string, unknown> } | null>
}

/** The generation's scans (ids). */
export interface StaticFilterStore { source: HitSource; scans: () => Promise<string[]>; gen: string }

/** A suffix range read whole above this many rows (row-group granular) is declined: V = 100K (the
 *  catalog's membership bound, so every non-member fits) plus two 8K-row groups of slack. */
export const MAX_ROWS = 100_000 + 2 * 8192

/** Hits held per isolate, across literals (each ~150 B): the oldest literal is dropped past this. */
const HELD_HITS = 400_000

const under = (p: string, root: string): boolean => root === '' || p.startsWith(root + '/')

/** The light source: the literal's suffix range from the shards, folded to its first hits once (per
 *  isolate, and per colo through `cache`), then cut to `under`. Literals over `maxRows` go to `heavy`. */
export class SuffixHits implements HitSource {
  private held = new Map<string, Promise<{ hits: Hit[]; io: Record<string, unknown> } | null>>()
  constructor(
    readonly names: StaticNames,
    readonly opts: { maxRows?: number; heavy?: HitSource | null; cache?: HitCache | null; waitMs?: number } = {},
  ) {}

  /** Every first hit of `key` (null: over `maxRows`, the heavy source's business). */
  all(key: string): Promise<{ hits: Hit[]; io: Record<string, unknown> } | null> {
    const p = shared(this.held, key, async () => {
      const cached = await this.opts.cache?.get(key)
      if (cached) return { hits: cached, io: { from: 'cache' } }
      const { io, fold } = await this.names.read(key, this.opts.maxRows ?? MAX_ROWS)
      if (!fold) return null
      await this.opts.cache?.put(key, fold.hits)
      return { hits: fold.hits, io: { from: 'shards', shard: io.shard, groups: io.groups, bytes: io.bytes, rows_read: io.rows_read, ms: io.ms } }
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

  async hits(key: string, root: string): Promise<{ hits: Hit[]; io: Record<string, unknown> } | null> {
    const got = await this.all(key)
    if (!got) return this.opts.heavy ? this.opts.heavy.hits(key, root) : null
    return { hits: root === '' ? got.hits : got.hits.filter(h => under(h.path, root)), io: got.io }
  }
}

/** A literal's first hits across isolates (the colo's Cache API). */
export interface HitCache { get(key: string): Promise<Hit[] | null>; put(key: string, hits: Hit[]): Promise<void> }

/** Hits as columns of JSON (sizes as decimal strings: a bucket's bytes pass 2^53). */
export function cacheHits(cache: Cache, prefix = STATIC_PREFIX, version = 'hits-v1'): HitCache {
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

/** The per-member "roots by path" source for literals over `MAX_ROWS` (not built yet: none). */
let heavySource: HitSource | null = null
/** Register the heavy literals' source (the per-member roots-by-path files). */
export function setHeavySource(s: HitSource | null): void { heavySource = s }

export type StaticFilterEnv = { FILTER_STATIC?: string; INDEX_R2?: R2Bucket }

/** A test's store for an env object (in place of the R2 binding's). */
export const injectedStores = new WeakMap<object, StaticFilterStore>()

let held: { r2: R2Bucket; store: StaticFilterStore } | undefined
/** The isolate's static filter store over the bound bucket; null when the static filter is off. A test
 *  injects its own (`injectedStores`). */
export function staticFilterStore(env: StaticFilterEnv): StaticFilterStore | null {
  const injected = injectedStores.get(env)
  if (injected) return injected
  if (env.FILTER_STATIC !== '1' || !env.INDEX_R2) return null
  if (held?.r2 !== env.INDEX_R2) {
    const blobs = r2Blobs(env.INDEX_R2)
    const names = new StaticNames(blobs, cacheIndexes(caches.default))
    held = { r2: env.INDEX_R2, store: { source: new SuffixHits(names, { cache: cacheHits(caches.default), get heavy() { return heavySource } }), scans: scanList(blobs), gen: STATIC_GEN } }
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

/** The hits live on `date` (scan id), as index rows of their owner slices (`usr` '' → null). Only
 *  `path, depth, usr, size, n_files` are known; the rest is left empty (a fold, not a row's word). */
export function liveRows(hits: Hit[], date: string): Row[] {
  const D = scanMs(date)
  const out: Row[] = []
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt)) continue
    out.push({
      path: h.path, depth: h.depth, usr: h.usr === '' ? null : h.usr, kind: null as unknown as Row['kind'],
      size: Number(h.size), n_files: Number(h.n), n_children: null, n_desc: null, mtime: null,
      mtime_mean: null, mtime_w: 0, last_read: null, cls2: 0, cls3: 0, cls4: 0,
    })
  }
  return out
}

/** The filter's bytes and objects under the hits' root on `date` (Σ live first hits), with `keep` an
 *  owner test on the slice's `usr` (null = unowned). */
export function liveTotal(hits: Hit[], date: string, keep: (usr: string | null) => boolean = () => true): { b: number; o: number; roots: number } {
  const D = scanMs(date)
  let b = 0n, o = 0n
  const roots = new Set<string>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt) || !keep(h.usr === '' ? null : h.usr)) continue
    b += h.size; o += h.n; roots.add(h.path)
  }
  return { b: Number(b), o: Number(o), roots: roots.size }
}

/** The cache keys' static marker: the generation when the static filter would answer this query's literal
 *  (so a response never outlives a switch of backend or generation), else ''. */
export function staticTag(env: StaticFilterEnv, query: { ast?: QueryAst } | undefined): string {
  return staticLiteral(query?.ast) && staticFilterStore(env) ? staticFilterStore(env)!.gen : ''
}
