/** The page's scope axes, applied server-side to a view's rows
 * (specs/view-serving.md §2): the owner axis (`o=`), and the name filter
 * (`q=`). The mark axis (`k=`) lives in `fates.ts` — it needs the ledger.
 *
 * Both are pure functions over index rows / per-path aggregates so `buildView`
 * stays one pipeline: read a superset by total bytes, narrow per row, fold. */

/** `o=`: `owned` = rows some person owns (`usr` set), `unowned` = the
 * NULL-`usr` slices (ownership has one axis: a person or nobody). `{not}` =
 * owned, but by nobody in the excluded set — the "owned by someone other than
 * these users" pool (`o=!<id>,<id>`), used to surface the paths a sweep would
 * touch that its sweeper does NOT own. Index rows are owner slices, so all
 * three are exact per path. */
export type OwnerScope = 'owned' | 'unowned' | { not: string[] }

// `claimed` / `unclaimed` were the pools' names until 2026-09-07; old links
// and cached clients still send them. `!a,b` = owned-except-{a,b}.
export const parseOwner = (raw: string | null): OwnerScope | undefined =>
  raw === 'owned' || raw === 'claimed' ? 'owned'
  : raw === 'unowned' || raw === 'unclaimed' ? 'unowned'
  : raw?.startsWith('!') ? { not: raw.slice(1).split(',').filter(Boolean) }
  : undefined

export const ownerOk = (usr: string | null, o: OwnerScope | undefined): boolean => {
  if (!o) return true
  if (o === 'owned') return usr != null
  if (o === 'unowned') return usr == null
  return usr != null && !o.not.includes(usr) // owned, excluding o.not
}

/** `q=`: `/…/` = regex (case-insensitive); anything else = substring (ci),
 * with `|` splitting alternatives. Predicates receive the index path
 * (`bucket/dir/sub`), so `grug/swarm` matches across segments. Mirrors the
 * client's `parseQuery` (`site/src/filterTree.ts`) exactly. */
export type NamePred = (path: string) => boolean

export function parseQuery(q: string | null): NamePred | null {
  const s = (q ?? '').trim()
  if (!s) return null
  if (s.length > 2 && s.startsWith('/') && s.endsWith('/')) {
    try {
      const re = new RegExp(s.slice(1, -1), 'i')
      return path => re.test(path)
    } catch {
      return null
    }
  }
  const needles = s.toLowerCase().split('|').map(t => t.trim()).filter(Boolean)
  if (!needles.length) return null
  return path => {
    const p = path.toLowerCase()
    return needles.some(t => p.includes(t))
  }
}

/** Name-filter a set of per-path aggregates (every path under the view root
 * that was read, plus the root itself as `''`-keyed `root`): the outermost
 * matching paths are the *match roots*; a path's filtered value is the sum of
 * the match roots at-or-under it (its own whole aggregate when it matches).
 * Returns the filtered aggregates (paths with zero filtered value dropped)
 * and the match-root set, so the response can flag them for bulk actions.
 *
 * Bytes are matched bytes only, never double-counted: a match root's
 * ancestors get its aggregate once, its descendants are not visited. Depth
 * is bounded by what was read — matches below the read's threshold are not
 * found (a fleet-wide deep search is a separate index; view-serving.md §2). */
export function nameFilter<A>(
  root: { path: string; agg: A },
  aggs: Map<string, { depth: number; agg: A }>,
  pred: NamePred,
  sum: (into: A, from: A) => void,
  empty: () => A,
): { root: A; aggs: Map<string, { depth: number; agg: A }>; matches: Set<string> } {
  const parentOf = (p: string): string => {
    const cut = p.lastIndexOf('/')
    return cut === -1 ? '' : p.slice(0, cut)
  }
  const matches = new Set<string>()
  if (pred(root.path)) {
    // The root itself matches: everything under it is matched bytes.
    matches.add(root.path)
    return { root: root.agg, aggs, matches }
  }
  // Outermost matches: a path whose every strict ancestor (down to the root)
  // fails the predicate. Ancestors of a read path are read too (descendant-
  // inclusive bytes are monotone), so walking up through `aggs` suffices.
  const isMatchRoot = (p: string): boolean => {
    if (!pred(p)) return false
    for (let a = parentOf(p); a !== root.path && a.length >= root.path.length; a = parentOf(a)) {
      if (pred(a)) return false
      if (a === '') break
    }
    return true
  }
  for (const p of aggs.keys()) if (isMatchRoot(p)) matches.add(p)
  // Filtered value per path = Σ match roots at-or-under it: seed each match
  // root with its own aggregate, then add it to every ancestor up to the root.
  const out = new Map<string, { depth: number; agg: A }>()
  const rootAgg = empty()
  for (const m of matches) {
    const src = aggs.get(m)!
    let p = m
    while (true) {
      let e = out.get(p)
      if (!e) out.set(p, (e = { depth: aggs.get(p)!.depth, agg: empty() }))
      sum(e.agg, src.agg)
      const par = parentOf(p)
      if (par === root.path || (par === '' && root.path === '')) break
      if (!aggs.has(par)) break
      p = par
    }
    sum(rootAgg, src.agg)
  }
  return { root: rootAgg, aggs: out, matches }
}
