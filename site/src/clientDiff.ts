import type { DiffData, DiffRow } from './DiffTreemap'
import type { TreeNode } from './types'

// Client-side diff for arbitrary scan pairs: align two scans' pixel-budget
// `tree.json`s by child name and emit the same `DiffData` rows the precomputed
// `diff.json` carries. The precomputed N-1→N pair comes from the batch job's
// exact best-first walk over the full listing; this aligner is the interactive
// fallback when the viewer picks a different "before" scan — each side's tree
// is that date's own budget fold, so small dirs may hide inside "(other)"
// tiles, which are merged into one aligned pseudo-child per parent (their
// combined delta stays truthful even when membership shifted).

const isFold = (n: TreeNode) => n.n.startsWith('(')

interface Side {
  b: number
  o: number
  kids?: TreeNode[]
}

function kidsByName(kids: TreeNode[] | undefined): { map: Map<string, TreeNode>; fold: Side | null } {
  const map = new Map<string, TreeNode>()
  let fb = 0
  let fo = 0
  let sawFold = false
  for (const k of kids ?? []) {
    if (isFold(k)) {
      sawFold = true
      fb += k.b
      fo += k.o
    } else map.set(k.n, k)
  }
  return { map, fold: sawFold ? { b: fb, o: fo } : null }
}

export function clientDiff(
  prev: TreeNode,
  curr: TreeNode,
  prevId: string,
  currId: string,
  top = 500,
): DiffData {
  const rows: DiffRow[] = []
  let expansions = 0

  const emit = (p: string, d: number, a: Side | null, b: Side | null, expanded: boolean) => {
    const s: DiffRow['s'] = !a ? 'added' : !b ? 'removed' : a.b !== b.b || a.o !== b.o ? 'changed' : 'unchanged'
    rows.push({
      p, d, k: 'dir', s,
      a: a?.b ?? 0, b: b?.b ?? 0, oa: a?.o ?? 0, ob: b?.o ?? 0,
      ...(expanded ? { x: true } : {}),
    })
  }

  const walk = (a: TreeNode | null, b: TreeNode | null, path: string, depth: number) => {
    // one-sided nodes are wholesale added/removed — no recursion needed, the
    // whole subtree's bytes ride on this row
    const expand = !!(a?.c?.length && b?.c?.length)
    if (path) emit(path, depth, a, b, expand)
    if (!expand) return
    expansions++
    const A = kidsByName(a!.c)
    const B = kidsByName(b!.c)
    for (const name of new Set([...A.map.keys(), ...B.map.keys()])) {
      walk(A.map.get(name) ?? null, B.map.get(name) ?? null, path ? `${path}/${name}` : name, depth + 1)
    }
    if (A.fold || B.fold) emit(path ? `${path}/(other)` : '(other)', depth + 1, A.fold, B.fold, false)
  }

  walk(prev, curr, '', 0)

  // Cap like the batch walk does: keep every expanded ancestor (the tree
  // skeleton) plus the top-|Δ| changed frontier rows; unchanged rows are
  // inferred as filler by the renderer.
  const skeleton = rows.filter(r => r.x)
  const frontier = rows
    .filter(r => !r.x && r.s !== 'unchanged')
    .sort((r1, r2) => Math.abs(r2.b - r2.a) - Math.abs(r1.b - r1.a))
  const kept = [...skeleton, ...frontier.slice(0, top)]

  return {
    prev: prevId,
    curr: currId,
    total_a: prev.b,
    total_b: curr.b,
    objects_a: prev.o,
    objects_b: curr.o,
    expansions,
    truncated: frontier.length > top,
    rows: kept,
  }
}
