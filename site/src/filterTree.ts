// In-memory treemap filter over the loaded (depth-capped) tree: keep the
// outermost nodes whose *segment name* matches (their whole subtree comes
// along), prune everything else, and re-aggregate ancestors from what
// survived — so sizes/attribution shown are matched bytes only, never
// double-counted. Segment-local semantics match disk-tree's `/api/filter`;
// filtering *below* the tree's depth cap is the vocab-sidecar / lakehouse
// work and arrives later.
import type { TreeNode } from './types'

export type NamePred = (name: string) => boolean

/** `/…/` = regex (case-insensitive); anything else = substring (ci). */
export function parseQuery(q: string): NamePred | null {
  const s = q.trim()
  if (!s) return null
  if (s.length > 2 && s.startsWith('/') && s.endsWith('/')) {
    try {
      const re = new RegExp(s.slice(1, -1), 'i')
      return name => re.test(name)
    } catch {
      return null
    }
  }
  const needle = s.toLowerCase()
  return name => name.toLowerCase().includes(needle)
}

export function filterTree(n: TreeNode, pred: NamePred): TreeNode | null {
  if (!n.n.startsWith('(') && pred(n.n)) return n
  const kids = (n.c ?? []).map(c => filterTree(c, pred)).filter((c): c is TreeNode => c != null)
  if (!kids.length) return null
  const tm: Record<string, number> = {}
  const sh: Record<string, number> = {}
  const us: Record<string, number> = {}
  let b = 0
  let o = 0
  let wd = 0
  let wdb = 0
  for (const k of kids) {
    b += k.b
    o += k.o
    for (const [t, tb] of Object.entries(k.tm ?? {})) tm[t] = (tm[t] ?? 0) + tb
    for (const [t, tb] of Object.entries(k.sh ?? {})) sh[t] = (sh[t] ?? 0) + tb
    for (const [u, ub] of k.us ?? []) us[u] = (us[u] ?? 0) + ub
    if (k.d != null) {
      wd += k.d * k.b
      wdb += k.b
    }
  }
  const out: TreeNode = { ...n, b, o, c: kids }
  out.tm = Object.keys(tm).length ? tm : undefined
  out.sh = Object.keys(sh).length ? sh : undefined
  out.us = Object.keys(us).length
    ? (Object.entries(us).sort((a, c) => c[1] - a[1]) as [string, number][])
    : undefined
  out.d = wdb ? Math.round(wd / wdb) : undefined
  return out
}

/** Filter below the root (the root's own name never counts as a match). */
export function applyFilter(root: TreeNode, pred: NamePred): TreeNode {
  const kids = (root.c ?? []).map(bucket => filterTree(bucket, pred)).filter((c): c is TreeNode => c != null)
  const b = kids.reduce((s, k) => s + k.b, 0)
  const o = kids.reduce((s, k) => s + k.o, 0)
  return { ...root, b, o, c: kids }
}
