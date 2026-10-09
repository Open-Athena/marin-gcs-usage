/** The smallest exact set of prefixes covering a filter's match roots under a view path (`/api/filter-cover`):
 *  what a bulk action on the matches — assign, stage — takes, instead of one prefix per match.
 *
 *  A folder `A` is *full* when every object under it is under some match root: the match roots under `A`
 *  are disjoint (outermost), so that holds iff their summed objects equal `A`'s total objects, and their
 *  summed bytes `A`'s total bytes (bytes alone would let a zero-byte non-match through; objects alone
 *  suffice, bytes is the cross-check). Full folders are closed downward, so the cover is the antichain of
 *  outermost full folders plus the roots under none of them — the unique minimum: any exact prefix set must
 *  put at least one prefix inside each outermost full folder, and those are disjoint.
 *
 *  Bounds: one lookup round per depth that holds candidate folders (deepest first), each round only the
 *  candidates none of whose candidate children is already known not full (a folder holding non-matching
 *  objects poisons every ancestor). A lookup over budget (`null`) stops collapsing: the remaining
 *  candidates count as not full, so the cover is still exact — just not minimal there. A root of one object
 *  may be an object or a folder holding one: its kind is looked up too (a folder prefix ends in `/`, an
 *  object's key doesn't); one the lookup can't place is `kind: null`, and nothing acts on it. */

export interface CoverRoot { path: string; b: number; o: number }
export type Kind = 'file' | 'dir'
/** A path's total bytes and objects on the scan (every owner slice summed), and what it is. */
export interface PathTotal { b: number; o: number; kind: Kind }
/** Totals of `paths` (each a folder or object path below the store root), or null: over the read budget. */
export type Lookup = (paths: string[]) => Promise<Map<string, PathTotal> | null>

export interface CoverItem {
  path: string
  /** `file`: an object key; `dir`: a folder prefix; null: unknown (a one-object root the lookup couldn't place). */
  kind: Kind | null
  b: number
  o: number
  /** Match roots it covers (1 for a root itself). */
  roots: number
}

export interface Cover {
  items: CoverItem[]
  /** Folders whose totals were looked up. */
  looked: number
  /** Candidate folders left unchecked (a lookup over budget): the cover is exact but may not be minimal. */
  unchecked: number
}

export const depthOf = (p: string): number => (p === '' ? 0 : p.split('/').length)
const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }

/** The fullness test: matched objects and bytes equal the folder's totals. */
export const isFull = (m: { b: number; o: number }, t: { b: number; o: number }): boolean => m.o === t.o && m.b === t.b

/** The cover of `roots` (outermost match roots, disjoint) under `view`: folders collapse at depth ≥
 *  `minDepth` (and at or below `view`), never above. */
export async function coverSet(
  roots: CoverRoot[],
  view: string,
  lookup: Lookup,
  { minDepth = 1, full = isFull }: { minDepth?: number; full?: (m: { b: number; o: number }, t: { b: number; o: number }) => boolean } = {},
): Promise<Cover> {
  const floor = Math.max(minDepth, depthOf(view))
  // Each candidate folder's matched sums: every proper ancestor of a root, at or below the view, at ≥ floor.
  const sums = new Map<string, { b: number; o: number; n: number }>()
  for (const r of roots) {
    for (let a = parentOf(r.path); depthOf(a) >= floor; a = parentOf(a)) {
      if (view !== '' && a !== view && !a.startsWith(view + '/')) break
      const s = sums.get(a)
      if (s) { s.b += r.b; s.o += r.o; s.n++ } else sums.set(a, { b: r.b, o: r.o, n: 1 })
    }
  }
  const byDepth = new Map<number, string[]>()
  for (const a of sums.keys()) { const d = depthOf(a); byDepth.set(d, [...(byDepth.get(d) ?? []), a]) }
  const fullSet = new Set<string>(), notFull = new Set<string>()
  const poison = (a: string) => { for (let p = a; sums.has(p) && !notFull.has(p); p = parentOf(p)) notFull.add(p) }
  let looked = 0, unchecked = 0, stopped = false
  for (const d of [...byDepth.keys()].sort((x, y) => y - x)) {
    const ask = byDepth.get(d)!.filter(a => !notFull.has(a)).sort()
    if (!ask.length) continue
    const got = stopped ? null : await lookup(ask)
    if (!got) { stopped = true; unchecked += ask.length; for (const a of ask) poison(a); continue }
    looked += ask.length
    for (const a of ask) {
      const t = got.get(a)
      if (t && t.kind === 'dir' && full(sums.get(a)!, t)) fullSet.add(a)
      else poison(a)
    }
  }
  // One-object roots: object or folder.
  const ambiguous = roots.filter(r => r.o <= 1).map(r => r.path)
  const kinds = ambiguous.length ? await lookup(ambiguous) : new Map<string, PathTotal>()
  const outermostFull = (p: string): string | null => {
    let best: string | null = null
    for (let a = parentOf(p); sums.has(a); a = parentOf(a)) if (fullSet.has(a)) best = a
    return best
  }
  const items = new Map<string, CoverItem>()
  for (const r of roots) {
    const f = outermostFull(r.path)
    if (f) {
      const it = items.get(f)
      if (it) { it.b += r.b; it.o += r.o; it.roots++ } else items.set(f, { path: f, kind: 'dir', b: r.b, o: r.o, roots: 1 })
      continue
    }
    const kind: Kind | null = r.o > 1 ? 'dir' : kinds?.get(r.path)?.kind ?? null
    items.set(r.path, { path: r.path, kind, b: r.b, o: r.o, roots: 1 })
  }
  return { items: [...items.values()].sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)), looked, unchecked }
}

/** What an action names for an item: a folder's prefix (`…/`), an object's key; null for an unknown kind. */
export const itemPrefix = (scheme: string, it: { path: string; kind: Kind | null }): string | null =>
  it.kind === 'dir' ? `${scheme}${it.path}/` : it.kind === 'file' ? `${scheme}${it.path}` : null
