/** Anchored name search over the static index (specs/search-extensions.md §2; specs/anchored-search.md): `^q` (a
 *  name starts with `q`), `q$` (a name ends with `q`) and `^q$` (a name is `q`), exact on every scan the tiers cover.
 *
 *  Semantics: a path matches when some segment (lowercased) matches; the predicate is monotone down the tree, so the
 *  match roots under a view path P are the paths under P whose name matches and **none of whose ancestor segments
 *  match** — per segment, not "the parent path contains `q`" (`tomato.txt` under `big-tomato/` is a root of
 *  `^tomat`). Each root is one version of one owner slice, as the contains literal's first hits.
 *
 *  Where the rows are:
 *  - `q$`: the suffix shards' rows with `s == q` (a name's suffix from some position equal to `q` is exactly "the
 *    name ends with `q`"; one per version), sorted by `path` — so under P they are the key range
 *    `[(q, P/), (q, P0))`, bounded by whole row groups through the group index and the shards' `path` statistics
 *    (`PathKeys`). The suffix shards are its roots file; no copy is built.
 *  - `^q`, `^q$`: the **name index** (`names/`, the suffix shards' layout): one row per version, `s` = `/` + the
 *    lowercase name. `/` occurs in no segment, so `^q` is the prefix range `/q…` and `^q$` the exact key `/q`
 *    (sorted by path, so scoped like `q$`).
 *
 *  Dispatch, per (key, P):
 *  1. **Light**: the key's whole range (all tiers) ≤ `MAX_ROWS` rows → read once, folded to first hits, held per
 *     isolate and colo per stack version, cut to P (as `SuffixHits`). `q$` reads every tier of the light index
 *     (its sx), the name index's keys the anchored tiers.
 *  2. **Scoped** (`q$`, `^q$` past 1): over the anchored tiers, the groups meeting `[(k, P/), (k, P0))`; their rows
 *     ≤ `R + 4·Σ rg` (each tier's bound is at most its true rows plus four groups: two straddling P's range, two
 *     holding other keys at the ends of `k`'s run) → read, keeping the rows under P.
 *  3. **Rollup**: else `(k, P)` holds more than R rows over the tiers, so the builder (`static_anchors.py`) made its
 *     rollup — per child of P its running totals, K kept plus an exact remainder (the drill's layout,
 *     `anchors/{end,exact}-rollups-index.parquet`), stacked newest tier first (`DRILL_RULES.stack`). P = `''` has
 *     one too (children = buckets).
 *  `^q` past 1 declines (`term-too-common`): a common prefix needs a drill of its own (not built).
 *
 *  Tiers: the base and each run in the light index's manifest, up to the first run without `anchors/meta.json` (the
 *  run's name rows and rollups, written last) — their scans are the ones an anchored answer covers (`Found.scans`). */
import { type Found, type HitCache, type HitSource, MAX_ROWS } from './staticFilter.js'
import { DRILL_RULES, type DrillMeta, GroupFile, type Rollup, type RollupCell, type Top } from './staticDrill.js'
import { type Blobs, cmp, FirstHits, type Hit, type IndexCache, type GroupIndex, type PathKeys, type SxColumns, StaticNames, underRange } from './staticNames.js'
import { combineFolds, subBlobs, type Tiers, type TierState } from './staticRuns.js'
import type { FilterRejectCode } from './indexedOnly.js'

export const NAMES_DIR = 'names'
export const ANCHORS_DIR = 'anchors'

export type Mode = 'start' | 'end' | 'exact'
/** A term as the static index reads it: its lowercase literal and anchors. */
export interface StaticTerm { text: string; start: boolean; end: boolean }

/** The key a term travels under (`HitSource.hits`, cache keys): a plain literal itself, an anchored one marked with
 *  `/` — `/q` (`^q`), `q/` (`q$`), `/q/` (`^q$`). A static literal never holds `/`, so no key is both. */
export const termKey = (t: StaticTerm): string => `${t.start ? '/' : ''}${t.text}${t.end ? '/' : ''}`

/** A key's literal and mode (null: a plain contains literal). */
export function parseKey(key: string): { text: string; mode: Mode | null } {
  const start = key.startsWith('/'), end = key.length > 1 && key.endsWith('/')
  const text = key.slice(start ? 1 : 0, end ? -1 : undefined)
  return { text, mode: start && end ? 'exact' : start ? 'start' : end ? 'end' : null }
}

/** Whether a lowercase segment matches `text` in `mode` (null: contains). */
export const segmentMatches = (seg: string, text: string, mode: Mode | null): boolean =>
  mode === 'start' ? seg.startsWith(text) : mode === 'end' ? seg.endsWith(text) : mode === 'exact' ? seg === text : seg.includes(text)

/** Whether `path` (any case) holds the keyed term: some segment of its lowercase matches (a contains literal: the
 *  lowercase path contains it, which for a `/`-free literal is the same). The view root P holding it is the plain view. */
export function termInPath(key: string, path: string): boolean {
  const { text, mode } = parseKey(key)
  const l = path.toLowerCase()
  if (!mode) return l.includes(text)
  return path !== '' && l.split('/').some(seg => segmentMatches(seg, text, mode))
}

/** An anchored term's first-hit fold over suffix rows (`q$`, `s` = the name's suffixes) or name rows (`^q`, `^q$`,
 *  `s` = `/` + the name): per row, the key test on `s`, depth ≥ 1, the paths under `under` (a scoped read's groups
 *  hold other paths too), and no ancestor segment matching. Each version holds at most one such row, so no dedup. */
export class AnchoredHits extends FirstHits {
  readonly text: string
  readonly mode: Mode
  private lo: string | null = null
  private hi = ''
  constructor(key: string, under?: string) {
    super(key)
    const k = parseKey(key)
    if (!k.mode) throw new Error(`static anchors: ${JSON.stringify(key)} is not an anchored key`)
    this.text = k.text
    this.mode = k.mode
    if (under) [this.lo, this.hi] = underRange(under)
  }

  /** The row key a row is matched on: `q` (suffix rows) or `/q` (name rows). */
  get rowKey(): string { return this.mode === 'end' ? this.text : `/${this.text}` }

  override add(cols: SxColumns): void {
    const k = this.rowKey, exact = this.mode !== 'start', t = this.text, mode = this.mode
    this.rows_read += cols.path.length
    for (let i = 0; i < cols.path.length; i++) {
      const s = cols.s[i]
      if (exact ? s !== k : !s.startsWith(k)) continue
      const p = cols.path[i]
      if (this.lo != null && (cmp(p, this.lo) < 0 || cmp(p, this.hi) >= 0)) continue
      this.rows_matching++
      if (cols.depth[i] < 1) continue
      const slash = p.lastIndexOf('/')
      if (slash >= 0 && p.slice(0, slash).toLowerCase().split('/').some(seg => segmentMatches(seg, t, mode))) continue
      this.hits.push({ path: p, depth: cols.depth[i], usr: cols.usr[i], vf: Number(cols.vf[i]), vt: Number(cols.vt[i]), size: cols.size[i], n: cols.n_files[i] })
    }
  }
}

/** A tier's `anchors/meta.json`: R, K, the shards' row-group size `rg` (the dispatch slack), the rollup index's
 *  `idx_rg`, per rollup set its `row_groups` (`end_rollups`, `exact_rollups`), and a run's `scans`. */
export interface AnchorsMeta extends DrillMeta { scans?: string[] }

/** One tier's anchored readers: its name index and rollups (the base: `dir` null). */
class AnchorTier {
  private meta?: Promise<AnchorsMeta>
  readonly names: StaticNames
  readonly rollups: Record<'end' | 'exact', GroupFile>
  constructor(readonly blobs: Blobs, readonly dir: string | null, caches: AnchorCaches) {
    this.names = new StaticNames(subBlobs(blobs, NAMES_DIR), caches.index?.(dir, NAMES_DIR), undefined, 8, caches.keys?.(dir, NAMES_DIR))
    const anchors = subBlobs(blobs, ANCHORS_DIR)
    const meta = () => this.info()
    this.rollups = {
      end: new GroupFile(anchors, 'end', 'rollups', meta, caches.top?.(dir)),
      exact: new GroupFile(anchors, 'exact', 'rollups', meta, caches.top?.(dir)),
    }
  }

  info(): Promise<AnchorsMeta> {
    this.meta ??= this.blobs.json<AnchorsMeta>(`${ANCHORS_DIR}/meta.json`).then(m => {
      for (const k of ['R', 'K', 'rg', 'idx_rg'] as const) if (!Number.isSafeInteger(m[k]) || m[k] < 1) throw new Error(`static anchors: bad ${this.dir ?? 'base'} meta.json ${k}`)
      return m
    })
    this.meta.catch(() => { this.meta = undefined })
    return this.meta
  }
}

/** The colo caches of a tier's anchored indexes (`dir` null: the base; `sub` the shard set). */
export interface AnchorCaches {
  index?: (dir: string | null, sub: string) => IndexCache<GroupIndex> | undefined
  keys?: (dir: string | null, sub: string) => IndexCache<PathKeys> | undefined
  top?: (dir: string | null) => IndexCache<Top> | undefined
}

/** The anchored tiers of a light-index state: the base and the runs (oldest first) up to the first without
 *  `anchors/meta.json`, with the scans they cover and the light tiers they pair with. */
interface AnchorState { version: string; scans: string[]; tiers: AnchorTier[]; light: TierState }

/** Answers held per isolate, across (key, path) views: the oldest dropped past this many hits + cells. */
const HELD = 400_000

/** The anchored terms' `HitSource` (keys from `termKey`; plain literals are not its business). */
export class AnchoredSource implements HitSource {
  private tiers = new Map<string, AnchorTier>()
  private live = new Set<string>()
  private held = new Map<string, Promise<Found | null>>()
  private sizes = new Map<string, number>()
  private whyNot = new Map<string, FilterRejectCode>()
  readonly heavy = true
  /** `light`: the light index's tiers (their sx: `q$`'s rows; their manifest: the runs); `blobs` the generation's
   *  keys; `cache` whole-range hit lists across isolates. */
  constructor(readonly light: Tiers, readonly blobs: Blobs, readonly opts: { cache?: HitCache | null; caches?: AnchorCaches; maxRows?: number } = {}) {}

  private tier(dir: string | null): AnchorTier {
    const k = dir ?? ''
    let t = this.tiers.get(k)
    if (!t) { t = new AnchorTier(dir == null ? this.blobs : subBlobs(this.blobs, dir), dir, this.opts.caches ?? {}); this.tiers.set(k, t) }
    return t
  }

  private async isLive(dir: string): Promise<boolean> {
    if (this.live.has(dir)) return true
    if (!this.blobs.list) return false
    const key = `${dir}/${ANCHORS_DIR}/meta.json`
    const yes = (await this.blobs.list(key)).includes(key)
    if (yes) this.live.add(dir)
    return yes
  }

  /** The anchored stack over the light index's current state. */
  async state(): Promise<AnchorState> {
    const light = await this.light.state()
    const none = { version: 'none', scans: [], tiers: [], light }
    if (!light.tiers.length) return none
    try { await this.tier(null).info() } catch { return none }
    const runs = light.tiers.slice(1).map(t => t.dir!)
    const live = await Promise.all(runs.map(d => this.isLive(d)))
    const n = live.indexOf(false)
    const kept = n < 0 ? runs : runs.slice(0, n)
    const dropped = new Set((light.manifest?.runs ?? []).filter(r => !kept.includes(r.key)).flatMap(r => r.scans))
    return {
      version: kept.length ? `${light.version}~${kept[kept.length - 1]}` : `${light.version}~base`,
      scans: light.scans.filter(s => !dropped.has(s)),
      tiers: [this.tier(null), ...kept.map(d => this.tier(d))],
      light,
    }
  }

  why(key: string): FilterRejectCode | undefined { return this.whyNot.get(key) }

  async hits(key: string, under: string): Promise<Found | null> {
    const { text, mode } = parseKey(key)
    if (!mode || !text) return null
    const t0 = Date.now()
    const whole = await this.whole(key, mode)
    if (whole === null) return null
    if (whole !== 'heavy') {
      const hits = under === '' ? whole.hits : whole.hits.filter(h => h.path.startsWith(under + '/'))
      return { hits, io: { ...whole.io, ms: Date.now() - t0 }, scans: whole.scans }
    }
    if (mode === 'start') { this.whyNot.set(key, 'term-too-common'); return null }
    const st = await this.state()
    if (!st.tiers.length) return null
    const k = `${key}\0${under}@${st.version}`
    let p = this.held.get(k)
    if (p) { this.held.delete(k); this.held.set(k, p) } else {
      p = this.scoped(key, mode, under, st)
      this.held.set(k, p)
      p.then(f => this.trim(k, f), () => this.held.delete(k))
    }
    const f = await p
    return f && { ...f, io: { ...f.io, ms: Date.now() - t0 } }
  }

  private trim(k: string, f: Found | null): void {
    this.sizes.set(k, f?.hits?.length ?? f?.rollup?.cells.length ?? 0)
    let n = [...this.sizes.values()].reduce((s, x) => s + x, 0)
    for (const old of this.held.keys()) {
      if (n <= HELD || old === k) break
      n -= this.sizes.get(old) ?? 0
      this.held.delete(old); this.sizes.delete(old)
    }
  }

  private wholeHeld = new Map<string, Promise<Whole | 'heavy'>>()

  /** Step 1: the key's whole range when it is light — `q$` over every light tier, the name index's keys over the
   *  anchored tiers — held per stack version (and per colo through `cache`); `'heavy'` past `maxRows`, null when no
   *  tier is there to read. */
  private async whole(key: string, mode: Mode): Promise<Whole | 'heavy' | null> {
    const maxRows = this.opts.maxRows ?? MAX_ROWS
    const st = mode === 'end' ? null : await this.state()
    const light = st?.light ?? await this.light.state()
    const version = st ? st.version : light.version
    const scans = st ? st.scans : light.scans
    if (!(st ? st.tiers.length : light.tiers.length)) return null
    const vkey = `${key}@${version}`
    let p = this.wholeHeld.get(vkey)
    if (!p) {
      p = (async () => {
        const cached = await this.opts.cache?.get(vkey)
        if (cached) return { hits: cached, io: { from: 'cache', version }, scans }
        const k = mode === 'end' ? parseKey(key).text : `/${parseKey(key).text}`
        const fold = () => new AnchoredHits(key)
        const exact = mode !== 'start'
        const readers = st ? st.tiers.map(t => t.names) : light.tiers.map(t => t.names)
        const sels = await Promise.all(readers.map(r => r.select(k, newIo(), { exact })))
        const rows = sels.reduce((n, s) => n + (s?.rows ?? 0), 0)
        if (rows > maxRows) return 'heavy' as const
        const got = await Promise.all(readers.map(r => r.read(k, Infinity, { exact, fold })))
        const hits = combineFolds(key, got.map(g => g.fold!)).hits
        await this.opts.cache?.put(vkey, hits)
        return { hits, io: { from: mode === 'end' ? 'shards' : 'names', version, rows_read: got.reduce((n, g) => n + g.io.rows_read, 0), bytes: got.reduce((n, g) => n + g.io.bytes, 0), tiers: readers.length }, scans }
      })()
      this.wholeHeld.set(vkey, p)
      p.catch(() => this.wholeHeld.delete(vkey))
      while (this.wholeHeld.size > 64) this.wholeHeld.delete(this.wholeHeld.keys().next().value!)
    }
    return p
  }

  /** Steps 2–3 for `q$` / `^q$` under `under` over the anchored tiers. */
  private async scoped(key: string, mode: 'end' | 'exact', under: string, st: AnchorState): Promise<Found | null> {
    const text = parseKey(key).text
    const k = mode === 'end' ? text : `/${text}`
    // `q$` reads each tier's suffix shards (the light index's readers, same tiers), `^q$` its name index.
    const readers = mode === 'end' ? st.light.tiers.slice(0, st.tiers.length).map(t => t.names) : st.tiers.map(t => t.names)
    const metas = await Promise.all(st.tiers.map(t => t.info()))
    const bound = metas[0].R + 4 * metas.reduce((n, m) => n + m.rg, 0)
    const sels = await Promise.all(readers.map(r => r.select(k, newIo(), { exact: true, under })))
    const upper = sels.reduce((n, s) => n + (s?.rows ?? 0), 0)
    if (upper <= bound) {
      const fold = () => new AnchoredHits(key, under)
      const got = await Promise.all(readers.map(r => r.read(k, Infinity, { exact: true, under, fold })))
      const hits = combineFolds(key, got.map(g => g.fold!)).hits
      return { hits, io: { from: 'anchors', source: 'roots', upper, rows_read: got.reduce((n, g) => n + g.io.rows_read, 0), tiers: readers.length }, scans: st.scans }
    }
    const lo: [string, string] = [k, under], hi: [string, string] = [k, under + '\0']
    const io = { top: 'isolate' as const, index_reads: 0, index_bytes: 0, groups: 0, bytes: 0, rows_read: 0 }
    const parts = await Promise.all(st.tiers.map(async t => {
      const gf = t.rollups[mode]
      const sel = await gf.select(lo, hi, io)
      return sel.groups?.length ? gf.read(lo, hi, sel.groups, io) : []
    }))
    const got = DRILL_RULES.stack(parts)
    if (!got) throw new Error(`static anchors: (${JSON.stringify(key)}, ${JSON.stringify(under)}) bounds ${upper} rows but has no rollup`)
    const num = (v: unknown) => Number(v)
    const cells: RollupCell[] = got.cells.map(r => ({ kind: r.kind as 1 | 2, child: r.child as string, vf: num(r.vf) * 1000, b: r.b as bigint, o: r.o as bigint }))
    const rollup: Rollup = { dir: under, kept: num(got.head.vf), rows: num(got.head.b), children: num(got.head.o), cells }
    return { rollup, io: { from: 'anchors', source: 'rollup', upper, ...io, tiers: st.tiers.length }, scans: st.scans }
  }
}

type Whole = { hits: Hit[]; io: Record<string, unknown>; scans: string[] }

const newIo = () => ({ shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none' as const, ms: {} })
