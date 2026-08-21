// Tree walks behind the /mark tabs: collect the maximal subtrees that a
// review lens cares about (mine / unattributed / communal), so each tab is a
// ranked worklist of prefixes rather than a hunt through the treemap.
import { useQuery } from '@tanstack/react-query'
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
