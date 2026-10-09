/** The static name index's daily runs (specs/static-daily-append.md, on the `daily-append` branch): a base
 *  generation plus one small run per newer scan, each run its own little generation under
 *  `deltas/<first>[_<last>]/` (`shards.json`, `sx/`, `catalog/`), listed by the newest immutable
 *  `manifests/<D>.json`. Readers query every tier and merge:
 *
 *  - Suffix rows: a run holds the versions opened in it and **close records** (a version from an older tier,
 *    its rows with their final `vt`). Rows equal on `(s, path, usr, vf)` are one version's; the smallest `vt`
 *    wins. A first hit is decided per row by `s` and `path` alone, so combining the tiers' first hits by `(path,
 *    usr, vf)` with the smallest `vt` equals combining their rows; liveness (`vf ≤ D < vt`) is applied after.
 *  - Catalog: a member iff some tier holds its header; the newest tier's header (`rows`, and `n` = its cells
 *    across every tier) and the union of the tiers' cells.
 *
 *  Without a manifest (or a `list`-less `Blobs`) the tiers are the base alone: today's reader. Answers for a
 *  scan already indexed never change when a run lands (a later close sets `vt` after it), so only the hit
 *  lists, which span every date, are versioned by the manifest's date. */
import { type CatalogIo, type CatalogMeta, type Member, StaticCatalog } from './staticCatalog.js'
import { type Blobs, cmp, FirstHits, type Io, type IndexCache, type GroupIndex, StaticNames } from './staticNames.js'

export interface RunInfo { key: string; first: string; last: string; level: number; scans: string[] }
export interface Manifest { gen: string; date: string; base_scans?: number; scans: string[]; runs: RunInfo[] }

/** `Blobs` over a subdirectory of another's keys. */
export function subBlobs(b: Blobs, dir: string): Blobs {
  const k = (key: string) => `${dir}/${key}`
  return {
    range: (key, offset, length) => b.range(k(key), offset, length),
    suffix: (key, n) => b.suffix(k(key), n),
    json: key => b.json(k(key)),
    ...(b.list ? { list: (prefix: string) => b.list!(k(prefix)).then(keys => keys.map(x => x.slice(dir.length + 1))) } : {}),
  }
}

/** The readers of one tier (the base: `dir` null). */
export interface Tier { dir: string | null; names: StaticNames; catalog: StaticCatalog }
export interface TierState { version: string; scans: string[]; tiers: Tier[]; manifest: Manifest | null }
/** Make a tier's readers over its `Blobs` (index caches keyed by `dir`, so tiers never share an entry). */
export type MakeTier = (blobs: Blobs, dir: string | null) => Omit<Tier, 'dir'>

/** The newest manifest's tiers, re-listed at most every `ttlMs` (tier readers, and their isolate caches, are
 *  kept across refreshes). */
export class Tiers {
  private held = new Map<string, Tier>()
  private at = -Infinity
  private cur?: Promise<TierState>
  constructor(readonly blobs: Blobs, readonly make: MakeTier, readonly ttlMs = 300_000, readonly now: () => number = () => Date.now()) {}

  private tier(dir: string | null): Tier {
    const k = dir ?? ''
    let t = this.held.get(k)
    if (!t) { t = { dir, ...this.make(dir == null ? this.blobs : subBlobs(this.blobs, dir), dir) }; this.held.set(k, t) }
    return t
  }

  state(): Promise<TierState> {
    const t = this.now()
    if (this.cur && t - this.at < this.ttlMs) return this.cur
    const prev = this.cur
    this.at = t
    const p = (async (): Promise<TierState> => {
      const manifest = await latestManifest(this.blobs)
      if (!manifest) {
        const scans = (await this.blobs.json<{ scans: { id: string }[] }>('scans.json')).scans.map(s => s.id).sort()
        return { version: 'base', scans, tiers: [this.tier(null)], manifest: null }
      }
      return { version: manifest.date, scans: [...manifest.scans].sort(), tiers: [this.tier(null), ...manifest.runs.map(r => this.tier(r.key))], manifest }
    })()
    this.cur = p
    // A failed refresh keeps serving the last good state (and retries on the next call).
    p.catch(() => { if (this.cur === p) { this.cur = prev; this.at = -Infinity } })
    return p
  }
}

/** The greatest `manifests/<D>.json`, or null (none, or no `list`). */
export async function latestManifest(blobs: Blobs): Promise<Manifest | null> {
  if (!blobs.list) return null
  const keys = (await blobs.list('manifests/')).filter(k => /^manifests\/[^/]+\.json$/.test(k)).sort(cmp)
  if (!keys.length) return null
  const m = await blobs.json<Manifest>(keys[keys.length - 1])
  if (!Array.isArray(m.scans) || !Array.isArray(m.runs) || typeof m.date !== 'string') throw new Error(`static runs: bad ${keys[keys.length - 1]}`)
  return m
}

/** First-hit folds of one literal from several tiers, combined: per `(path, usr, vf)` the smallest `vt`. */
export function combineFolds(key: string, folds: FirstHits[]): FirstHits {
  const out = new FirstHits(key)
  const at = new Map<string, number>()
  for (const f of folds) {
    out.rows_read += f.rows_read
    out.rows_matching += f.rows_matching
    for (const h of f.hits) {
      const k = `${h.path}\0${h.usr}\0${h.vf}`
      const i = at.get(k)
      if (i == null) { at.set(k, out.hits.length); out.hits.push(h) } else if (h.vt < out.hits[i].vt) out.hits[i] = h
    }
  }
  return out
}

const sumIo = (ios: Io[]): Io => {
  const io: Io = { ...ios[0], ms: { ...ios[0].ms }, tiers: ios.length }
  for (const x of ios.slice(1)) {
    io.groups += x.groups; io.bytes += x.bytes; io.rows_read += x.rows_read; io.rows_matching += x.rows_matching
    for (const [k, v] of Object.entries(x.ms)) io.ms[k] = Math.max(io.ms[k] ?? 0, v)
  }
  return io
}

/** The suffix reader over every tier, with `StaticNames`' interface. */
export class TieredNames {
  constructor(readonly tiers: Tiers) {}

  async version(): Promise<string> { return (await this.tiers.state()).version }

  /** Σ over tiers of the rows `key`'s range spans (row-group granular): an upper bound on rows read, and since
   *  a range's rows only grow and close records only add, ≤ V still proves `key` is no catalog member. */
  async extent(key: string, io: Io): Promise<{ rows: number } | null> {
    const { tiers } = await this.tiers.state()
    const ios = tiers.map((): Io => ({ shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} }))
    const exts = await Promise.all(tiers.map((t, i) => t.names.extent(key, ios[i])))
    Object.assign(io, { shard: ios[0].shard, index: ios[0].index, tiers: tiers.length })
    if (exts.every(e => e == null)) return null
    io.groups = exts.reduce((a, e) => a + (e ? e.b - e.a : 0), 0)
    return { rows: exts.reduce((a, e) => a + (e?.rows ?? 0), 0) }
  }

  /** `key`'s first hits over every tier; `maxRows` refuses (null) when the tiers' summed extent is above it. */
  async read(key: string, maxRows = Infinity): Promise<{ io: Io; fold: FirstHits | null }> {
    const { tiers } = await this.tiers.state()
    if (maxRows !== Infinity) {
      const probe: Io = { shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} }
      const ext = await this.extent(key, probe)
      if (ext && ext.rows > maxRows) return { io: { ...probe, rows_read: ext.rows }, fold: null }
    }
    const got = await Promise.all(tiers.map(t => t.names.read(key)))
    return { io: sumIo(got.map(g => g.io)), fold: combineFolds(key, got.map(g => g.fold!)) }
  }

  async answer(key: string, dates: string[], maxRows = Infinity) {
    const { io, fold } = await this.read(key, maxRows)
    return { io, answer: fold ? fold.answer(dates) : null }
  }
}

/** The catalog over every tier, with `StaticCatalog`'s `info`/`lookup`. */
export class TieredCatalog {
  constructor(readonly tiers: Tiers) {}

  /** The base's `meta.json` (V is the generation's). */
  async info(): Promise<CatalogMeta> { return (await this.tiers.state()).tiers[0].catalog.info() }

  async lookup(q: string): Promise<{ io: CatalogIo; member: Member | null }> {
    const { tiers } = await this.tiers.state()
    const got = await Promise.all(tiers.map(t => t.catalog.lookup(q, { partial: tiers.length > 1 })))
    const io: CatalogIo = { ...got[0].io }
    for (const g of got.slice(1)) { io.groups += g.io.groups; io.bytes += g.io.bytes; io.rows_read += g.io.rows_read }
    const members = got.map(g => g.member).filter((m): m is Member => m != null)
    if (!members.length) return { io, member: null }
    const head = members[members.length - 1]
    const cells = members.flatMap(m => m.cells).sort((x, y) => cmp(x.bucket, y.bucket) || (x.vf < y.vf ? -1 : x.vf > y.vf ? 1 : 0))
    if (head.n != null && cells.length !== head.n) throw new Error(`static catalog: ${JSON.stringify(q)} header counts ${head.n} cells, the tiers hold ${cells.length}`)
    return { io, member: { q, rows: head.rows, cells } }
  }
}

/** The tiers' readers over one `Blobs`, with the given per-tier index caches. */
export function tiers(blobs: Blobs, opts: {
  indexCache?: (dir: string | null) => IndexCache<GroupIndex> | undefined
  catalogCache?: (dir: string | null) => IndexCache<import('./staticCatalog.js').CatalogIndex> | undefined
  clock?: () => Promise<number>
  ttlMs?: number
  now?: () => number
} = {}): { tiers: Tiers; names: TieredNames; catalog: TieredCatalog; scans: () => Promise<string[]> } {
  const t = new Tiers(blobs, (b, dir) => ({
    names: new StaticNames(b, opts.indexCache?.(dir), opts.clock),
    catalog: new StaticCatalog(b, opts.catalogCache?.(dir)),
  }), opts.ttlMs, opts.now)
  return { tiers: t, names: new TieredNames(t), catalog: new TieredCatalog(t), scans: async () => (await t.state()).scans }
}
