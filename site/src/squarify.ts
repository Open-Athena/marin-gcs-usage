import type { TreeNode } from './types'

export interface Rect {
  it: TreeNode
  x: number
  y: number
  w: number
  h: number
}

/** Squarified treemap layout (Bruls et al.). */
export function squarify(items: TreeNode[], x: number, y: number, w: number, h: number): Rect[] {
  const out: Rect[] = []
  items = items.filter(it => it.b > 0)
  const total = items.reduce((s, it) => s + it.b, 0)
  if (!total || w <= 0 || h <= 0) return out
  const scale = (w * h) / total
  let row: TreeNode[] = []
  let rowSum = 0
  const rest = items.slice()

  const worst = (r: TreeNode[], len: number): number => {
    const s = r.reduce((a, it) => a + it.b * scale, 0)
    let mn = Infinity
    let mx = 0
    for (const it of r) {
      const a = it.b * scale
      mn = Math.min(mn, a)
      mx = Math.max(mx, a)
    }
    const s2 = s * s
    const l2 = len * len
    return Math.max((l2 * mx) / s2, s2 / (l2 * mn))
  }

  const layoutRow = () => {
    const len = Math.min(w, h)
    const thick = rowSum / len
    let off = 0
    for (const it of row) {
      const l = (it.b * scale) / thick
      if (w <= h) out.push({ it, x: x + off, y, w: l, h: thick })
      else out.push({ it, x, y: y + off, w: thick, h: l })
      off += l
    }
    if (w <= h) {
      y += thick
      h -= thick
    } else {
      x += thick
      w -= thick
    }
  }

  while (rest.length) {
    const len = Math.min(w, h)
    const it = rest[0]
    if (!row.length || worst([...row, it], len) <= worst(row, len)) {
      row.push(rest.shift()!)
      rowSum += it.b * scale
    } else {
      layoutRow()
      row = []
      rowSum = 0
    }
  }
  if (row.length) layoutRow()
  return out
}

/** Fold items too small to render at this scale into one synthetic node, so
 * their combined area shows as a single tile instead of dropped rows of
 * sub-6px cells (which read as dead space). */
export function foldSmall(items: TreeNode[], w: number, h: number, minArea = 16): TreeNode[] {
  const vis = items.filter(it => it.b > 0)
  const total = vis.reduce((s, it) => s + it.b, 0)
  if (!total || w <= 0 || h <= 0) return vis
  const scale = (w * h) / total
  const kept = vis.filter(it => it.b * scale >= minArea)
  const tiny = vis.filter(it => it.b * scale < minArea)
  if (tiny.length < 2) return vis
  const b = tiny.reduce((s, it) => s + it.b, 0)
  const o = tiny.reduce((s, it) => s + it.o, 0)
  const tm: Record<string, number> = {}
  const us: Record<string, number> = {}
  let wd = 0
  let wdb = 0
  for (const it of tiny) {
    for (const [t, tb] of Object.entries(it.tm ?? {})) tm[t] = (tm[t] ?? 0) + tb
    for (const [u, ub] of it.us ?? []) us[u] = (us[u] ?? 0) + ub
    if (it.d != null) {
      wd += it.d * it.b
      wdb += it.b
    }
  }
  const folded: TreeNode = { n: `(+${tiny.length})`, b, o }
  if (wdb) folded.d = Math.round(wd / wdb)
  if (Object.keys(tm).length) folded.tm = tm
  const topUs = Object.entries(us).sort((a, c) => c[1] - a[1]).slice(0, 5)
  if (topUs.length) folded.us = topUs as [string, number][]
  return [...kept, folded]
}
