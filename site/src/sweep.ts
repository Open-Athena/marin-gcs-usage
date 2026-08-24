// Tree walks behind the /mark tabs: collect the maximal subtrees that a
// review lens cares about (mine / unattributed / communal), so each tab is a
// ranked worklist of prefixes rather than a hunt through the treemap.
import { useQuery } from '@tanstack/react-query'
import type { NodePred } from './filterTree'
import type { MarkIndex } from './marks'
import type { TreeNode } from './types'

export interface SweepRow {
  uri: string
  node: TreeNode
  /** Bytes the lens attributes here (mine / unattributed / communal). */
  b: number
  /** Share of the node the lens owns. */
  frac: number
}

/** Bytes the lens assigns to a node. */
export type Lens = (n: TreeNode) => number

export const userLens = (user: string): Lens => n => n.us?.find(([u]) => u === user)?.[1] ?? 0
export const teamLens = (team: string): Lens => n => n.tm?.[team] ?? 0

/**
 * Treemap-scoping predicate for a lens: keep the maximal subtrees the lens
 * owns (≥`minFrac` of the node), prune the rest, re-aggregate ancestors —
 * so a tab's map shows just that tab's data instead of dimming the estate.
 */
export const lensNodePred = (lens: Lens, minFrac = 0.6): NodePred => n => {
  const b = lens(n)
  return b > 0 && b >= minFrac * n.b
}

/**
 * Maximal nodes where the lens owns ≥`minFrac` of the node and ≥`minBytes`
 * absolute — don't descend into a collected node (its children are implied),
 * but do descend into mixed nodes to find the owned subtrees inside them.
 */
export function collectRows(
  root: TreeNode,
  lens: Lens,
  { minBytes = 100e9, minFrac = 0.6 }: { minBytes?: number; minFrac?: number } = {},
): SweepRow[] {
  const rows: SweepRow[] = []
  const walk = (n: TreeNode, uri: string) => {
    for (const c of n.c ?? []) {
      if (c.n.startsWith('(')) continue
      const cUri = `${uri}/${c.n}`
      const b = lens(c)
      if (b < minBytes) continue
      if (b >= minFrac * c.b) rows.push({ uri: cUri, node: c, b, frac: b / c.b })
      else walk(c, cUri)
    }
  }
  // root.c are buckets; bucket URIs are `gs://<bucket>`
  for (const bucket of root.c ?? []) walk(bucket, `gs://${bucket.n}`)
  return rows
}

/**
 * The keep-axis "to-do": the largest prefixes with no keep/sweep decision
 * anywhere in their subtree or ancestry (mirrors `functions/_lib/todo.ts`, so
 * the tab and `gcs-usage todo` agree). A keep decision inherits down, so a node
 * is a to-do item only when it's fully untouched; marking part of it drops it
 * and surfaces its still-clean siblings. Biggest first.
 */
export function collectTodo(root: TreeNode, idx: MarkIndex, minBytes = 20e9): SweepRow[] {
  const rows: SweepRow[] = []
  const walk = (n: TreeNode, uri: string) => {
    const { mark, under } = idx.resolve(uri)
    if (mark) return // decided on this prefix or an ancestor — whole subtree settled
    if (under === 0) {
      if (n.b >= minBytes) rows.push({ uri, node: n, b: n.b, frac: 1 })
      return // no decision anywhere below → a clean chunk; take it whole
    }
    for (const c of n.c ?? []) {
      if (!c.n.startsWith('(')) walk(c, `${uri}/${c.n}`)
    }
  }
  for (const bucket of root.c ?? []) walk(bucket, `gs://${bucket.n}`)
  return rows.sort((a, b) => b.b - a.b)
}

const CKPT_SEG_RE = /^(step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?\d+/i

/**
 * Checkpoint-shaped: under a checkpoints path segment, named like one, or
 * ≥2 step-numbered children. Gates the `keep_last_ckpt` button — offering
 * it on arbitrary dirs was confusing. (Scan post-proc will flag this
 * properly per specs/actions-ledger.md; these heuristics cover the interim.)
 */
export const looksCkpt = (n: TreeNode, uri?: string): boolean =>
  (!!uri && /\/(ckpts?|checkpoints?)(\/|$)/i.test(uri)) ||
  /(^|[-_.])(ckpts?|checkpoints?)([-_.]|$)/i.test(n.n) ||
  (n.c ?? []).filter(c => CKPT_SEG_RE.test(c.n)).length >= 2

/** Reviewed = covered by any mark (deepest-wins ancestor or own). */
export const reviewedBytes = (rows: SweepRow[], idx: MarkIndex): number =>
  rows.reduce((s, r) => s + (idx.resolve(r.uri).mark ? r.b : 0), 0)

/** The viewer's canonical attribution user id, from D1 `user_emails`. */
export function useMyUser(email: string | undefined, enabled: boolean): string | null {
  const { data } = useQuery<Record<string, string>, Error>({
    queryKey: ['user-emails'],
    enabled: enabled && !!email,
    staleTime: 10 * 60_000,
    retry: false,
    queryFn: async () => {
      const r = await fetch('/api/db/user_emails', { credentials: 'include' })
      if (!r.ok) throw new Error(`user_emails: ${r.status}`)
      const { rows } = (await r.json()) as { rows: { email: string; user: string }[] }
      return Object.fromEntries(rows.map(x => [x.email, x.user]))
    },
  })
  return (email && data?.[email.toLowerCase()]) || null
}
