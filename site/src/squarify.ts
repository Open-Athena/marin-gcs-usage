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
