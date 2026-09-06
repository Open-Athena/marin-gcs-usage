/** The mark axis (`k=` ⊆ `ksu`) applied per view node, server-side
 * (specs/view-serving.md §2): of a path's subtree bytes, how many sit under
 * an allowed fate.
 *
 * Inputs: the per-mark manifest the estate totals already compute per
 * `(scan, ledger head)` (`_lib/totals.ts`): every live mark's subtree bytes,
 * its *band* (bytes minus deeper marks) split by painted fate, and whether a
 * newer ancestor repaints it. From that, any path N's allowed bytes are:
 *
 *   value(N) = [fate(cover N) allowed] · (b(N) − Σ_{m ∈ top(N)} bytes(m))
 *            + Σ_{m strictly under N} Σ_{f allowed} net_f(m)
 *
 * where cover(N) is the newest ledger row at-or-above N — a mark or a clear
 * (its *effective* fate is N's residual's fate — recency beats specificity;
 * a newer clear above a mark repaints it unmarked), top(N) the outermost rows
 * strictly under N (their subtrees are not N's residual), and every mark
 * under N contributes its band under its *effective* fate (a repainted mark's
 * band carries the repainter's). Bands partition the subtree, so this is
 * exact; `keep_last_ckpt` counts as keep ∧ sweep where it can't be split,
 * matching the client's `fateAllowed`. */
import type { Fate, MarkRow } from './marks.js'
import { idxKey } from './marks.js'

export type FateAxis = 'keep' | 'sweep' | 'unmarked'
const LETTERS: Record<string, FateAxis> = { k: 'keep', s: 'sweep', u: 'unmarked' }

/** `k=ksu` letters → the allowed set; all/none/absent → undefined (no scope). */
export function parseFates(raw: string | null): Set<FateAxis> | undefined {
  if (!raw) return undefined
  const out = new Set<FateAxis>()
  for (const ch of raw) if (LETTERS[ch]) out.add(LETTERS[ch])
  return out.size === 0 || out.size === 3 ? undefined : out
}

export const fateAllowed = (fate: Fate, allowed: ReadonlySet<FateAxis>): boolean =>
  fate === 'keep_last_ckpt' ? allowed.has('keep') || allowed.has('sweep') : allowed.has(fate as FateAxis)

interface Mark {
  path: string // index key (`marin-b/x/y`)
  ts: number
  eff: Fate // effective fate of its band (own keep, a repainter's, or unmarked for a clear)
  bytes: number
  net: Record<Fate, number>
}

export interface FateScope {
  /** Bytes of `path`'s subtree (total `b`) that sit under an allowed fate. */
  value(path: string, b: number): number
}

export function fateScope(marks: MarkRow[], allowed: ReadonlySet<FateAxis>): FateScope {
  const all: Mark[] = marks
    .map(m => ({ path: idxKey(m.prefix).path, ts: m.ts, eff: m.eff, bytes: m.bytes, net: m.net }))
    .sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  const byPath = new Map(all.map(m => [m.path, m]))
  const parentOf = (p: string): string => {
    const cut = p.lastIndexOf('/')
    return cut === -1 ? '' : p.slice(0, cut)
  }
  // Nearest strict ancestor mark per mark (null = none): a mark under N is
  // one of N's *top* marks iff its nearest ancestor mark is not under N.
  const anc = new Map<string, Mark | null>()
  for (const m of all) {
    let a: Mark | null = null
    for (let q = parentOf(m.path); ; q = parentOf(q)) {
      const hit = byPath.get(q)
      if (hit) { a = hit; break }
      if (q === '') break
    }
    anc.set(m.path, a)
  }
  const netAllowed = (m: Mark): number => {
    let s = 0
    for (const f of Object.keys(m.net) as Fate[]) if (m.net[f] > 0 && fateAllowed(f, allowed)) s += m.net[f]
    return s
  }
  // First index in the sorted list whose path is >= `key`.
  const lowerBound = (key: string): number => {
    let lo = 0
    let hi = all.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (all[mid].path < key) lo = mid + 1
      else hi = mid
    }
    return lo
  }
  return {
    value(path: string, b: number): number {
      // cover: the newest mark on N's ancestor-or-self chain (recency wins).
      let cover: Mark | null = null
      for (let q = path; ; q = parentOf(q)) {
        const m = byPath.get(q)
        if (m && (!cover || m.ts > cover.ts)) cover = m
        if (q === '') break
      }
      // marks strictly under N: one contiguous run of the sorted list.
      const pfx = path === '' ? '' : path + '/'
      let sumNet = 0
      let topBytes = 0
      for (let i = lowerBound(pfx); i < all.length; i++) {
        const m = all[i]
        if (pfx && !m.path.startsWith(pfx)) break
        if (m.path === path) continue
        sumNet += netAllowed(m)
        const a = anc.get(m.path) ?? null
        if (!a || a.path.length <= path.length) topBytes += m.bytes
      }
      const residual = Math.max(0, b - topBytes)
      const coverFate: Fate = cover?.eff ?? 'unmarked'
      return (fateAllowed(coverFate, allowed) ? residual : 0) + sumNet
    },
  }
}
