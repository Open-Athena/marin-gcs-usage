// Mark & sweep data plumbing (specs/mark-sweep-ui.md): TSQ bindings for
// /api/marks + /api/claims, and the deepest-mark-wins resolver the treemap
// overlay uses. Marks are dir prefixes (`gs://bucket/path/`); resolution for
// a node = the deepest mark whose prefix contains it. `delete` is the default
// (absence of a mark) — explicit rows exist to carve children out of a kept
// parent, or to record an affirmative decision.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

export type MarkAction = 'keep' | 'keep_last_ckpt' | 'delete'

export interface Mark {
  prefix: string
  action: MarkAction
  who: string
  ts: number
  note: string | null
}

export interface Claim {
  prefix: string
  who: string
  ts: number
}

export const ACTION_LABELS: Record<MarkAction, string> = {
  keep: 'keep',
  keep_last_ckpt: 'keep last ckpt',
  delete: 'delete',
}

/** Sweep deadline: EOD Friday 2026-08-28, Pacific (Percy's post governs). */
export const SWEEP_DEADLINE = new Date('2026-08-29T00:00:00-07:00')

export const sweepDaysLeft = (): number =>
  Math.max(0, Math.ceil((SWEEP_DEADLINE.getTime() - Date.now()) / 86_400_000))

// 30s poll: several people mark concurrently during the sprint, and the
// overlay should reflect their marks without a reload.
export function useMarks(enabled: boolean) {
  return useQuery<{ marks: Mark[]; claims: Claim[] }, Error>({
    queryKey: ['marks'],
    enabled,
    refetchInterval: 30_000,
    queryFn: async () => {
      const r = await fetch('/api/marks', { credentials: 'include' })
      if (!r.ok) throw new Error(`marks: ${r.status}`)
      return r.json()
    },
  })
}

export function useMarkMutations() {
  const qc = useQueryClient()
  const done = () => void qc.invalidateQueries({ queryKey: ['marks'] })
  const put = useMutation({
    mutationFn: async (v: { prefix: string; action: MarkAction | null; note?: string }) => {
      const r = await fetch('/api/marks', {
        method: 'PUT',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(v),
      })
      if (!r.ok) throw new Error(((await r.json()) as { error?: string }).error ?? `${r.status}`)
    },
    onSuccess: done,
  })
  const claim = useMutation({
    mutationFn: async (v: { prefix: string; release?: boolean }) => {
      const r = await fetch('/api/claims', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(v),
      })
      if (!r.ok) throw new Error(((await r.json()) as { error?: string }).error ?? `${r.status}`)
    },
    onSuccess: done,
  })
  return { put, claim }
}

export interface MarkState {
  /** Deepest mark covering this node (its effective fate), if any. */
  mark: Mark | null
  /** The mark sits exactly on this node (vs inherited from an ancestor). */
  own: boolean
  /** Marks strictly inside this node's subtree (drill to see them). */
  under: number
}

export interface MarkIndex {
  resolve: (uri: string) => MarkState
  claimOf: (uri: string) => Claim | null
  count: number
}

/**
 * O(marks) per lookup — the table is thousands of rows at most and lookups
 * run per rendered cell (~hundreds), so brute force beats maintaining a trie.
 */
export function useMarkIndex(data: { marks: Mark[]; claims: Claim[] } | undefined): MarkIndex {
  return useMemo(() => {
    const marks = data?.marks ?? []
    const claims = new Map((data?.claims ?? []).map(c => [c.prefix, c]))
    const resolve = (uri: string): MarkState => {
      const p = uri.endsWith('/') ? uri : uri + '/'
      let mark: Mark | null = null
      let under = 0
      for (const m of marks) {
        if (p.startsWith(m.prefix)) {
          if (!mark || m.prefix.length > mark.prefix.length) mark = m
        } else if (m.prefix.startsWith(p)) under++
      }
      return { mark, own: mark?.prefix === p, under }
    }
    const claimOf = (uri: string) => claims.get(uri.endsWith('/') ? uri : uri + '/') ?? null
    return { resolve, claimOf, count: marks.length }
  }, [data])
}
