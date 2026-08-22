// Actions-ledger data plumbing (specs/actions-ledger.md): TSQ bindings for
// /api/actions and the most-recent-wins resolver the treemap overlay uses.
// The ledger is an append-only WAL of actions; the API serves the live
// expanded prefix rows per axis (keep / owner), and this module folds them:
// for a path, the effective value per axis is the most recent live row on an
// ancestor-or-equal prefix (recency beats specificity — a newer broad mark
// repaints older deeper ones). `sweep` is the default fate (absence of a
// mark) — explicit rows record affirmative decisions, incl. clears.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo } from 'react'

export type MarkAction = 'keep' | 'keep_last_ckpt' | 'sweep'

export interface KeepRow {
  prefix: string
  keep: MarkAction | null
  ts: number
  who: string
  memo: string | null
  action_id: number
}

export interface OwnerRow {
  prefix: string
  owner: string | null
  ts: number
  who: string
  memo: string | null
  action_id: number
}

/** The resolved (effective) keep-state of a prefix, with provenance. */
export interface Mark {
  prefix: string
  action: MarkAction
  who: string
  ts: number
  note: string | null
}

export interface Owner {
  prefix: string
  /** Canonical user id (or email, for pre-mapping claims). */
  who: string
  ts: number
}

export const ACTION_LABELS: Record<MarkAction, string> = {
  keep: 'keep',
  keep_last_ckpt: 'keep last ckpt',
  sweep: 'delete',
}

/** Sweep deadline: EOD Friday 2026-08-28, Pacific (Percy's post governs). */
export const SWEEP_DEADLINE = new Date('2026-08-29T00:00:00-07:00')

export const sweepDaysLeft = (): number =>
  Math.max(0, Math.ceil((SWEEP_DEADLINE.getTime() - Date.now()) / 86_400_000))

// 30s poll: several people mark concurrently during the sprint, and the
// overlay should reflect their marks without a reload.
export function useMarks(enabled: boolean) {
  return useQuery<{ keeps: KeepRow[]; owners: OwnerRow[] }, Error>({
    queryKey: ['actions'],
    enabled,
    refetchInterval: 30_000,
    queryFn: async () => {
      const r = await fetch('/api/actions', { credentials: 'include' })
      if (!r.ok) throw new Error(`actions: ${r.status}`)
      return r.json()
    },
  })
}

/** One POSTable action; omitted axis = untouched, null value = clear. */
export interface ActionPost {
  pattern: string
  keep?: MarkAction | null
  /** `'@me'` = the server resolves the actor's canonical user id. */
  owner?: string | null
  memo?: string
  scan?: string
}

// The scan id the viewer is looking at, stamped onto posted actions. Set by
// App (module-level: mutations fire from deep components that don't
// otherwise care which scan is showing).
let currentScan: string | undefined
export const setCurrentScan = (s?: string) => { currentScan = s }

export function useMarkMutations() {
  const qc = useQueryClient()
  const done = () => void qc.invalidateQueries({ queryKey: ['actions'] })
  const post = useMutation({
    mutationFn: async (v: ActionPost | ActionPost[]) => {
      const items = (Array.isArray(v) ? v : [v]).map(a => ({ scan: currentScan, ...a }))
      const r = await fetch('/api/actions', {
        method: 'POST',
        credentials: 'include',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(items.length === 1 ? items[0] : items),
      })
      if (!r.ok) throw new Error(((await r.json()) as { error?: string }).error ?? `${r.status}`)
    },
    onSuccess: done,
  })
  // Compat surfaces for the mark/claim call sites.
  const put = {
    mutate: (v: { prefix: string; action: MarkAction | null; note?: string }, opts?: { onSuccess?: () => void }) =>
      post.mutate({ pattern: v.prefix, keep: v.action, memo: v.note }, opts),
    error: post.error,
  }
  const claim = {
    mutate: (v: { prefix: string; release?: boolean }) =>
      post.mutate({ pattern: v.prefix, owner: v.release ? null : '@me' }),
    error: post.error,
  }
  return { put, claim, post }
}

export interface MarkState {
  /** Effective fate: most recent live row on an ancestor-or-equal prefix. */
  mark: Mark | null
  /** The winning mark sits exactly on this prefix (vs inherited). */
  own: boolean
  /** Marked prefixes strictly inside this subtree (drill to see them). */
  under: number
}

export interface MarkIndex {
  resolve: (uri: string) => MarkState
  claimOf: (uri: string) => Owner | null
  /** Live set-marks strictly under a prefix — what a broad mark would repaint. */
  overridesOf: (uri: string) => { n: number; keeps: number }
  count: number
}

const newer = (a: { ts: number; action_id: number }, b: { ts: number; action_id: number }) =>
  a.ts > b.ts || (a.ts === b.ts && a.action_id > b.action_id)

/** Latest live row per prefix (the API may return history rows per prefix). */
function foldLatest<R extends { prefix: string; ts: number; action_id: number }>(rows: R[]): Map<string, R> {
  const m = new Map<string, R>()
  for (const r of rows) {
    const cur = m.get(r.prefix)
    if (!cur || newer(r, cur)) m.set(r.prefix, r)
  }
  return m
}

/**
 * O(prefixes) per lookup — thousands of rows at most, lookups run per
 * rendered cell (~hundreds), so brute force beats maintaining a trie.
 */
export function useMarkIndex(data: { keeps: KeepRow[]; owners: OwnerRow[] } | undefined): MarkIndex {
  return useMemo(() => {
    const keeps = foldLatest(data?.keeps ?? [])
    const owners = foldLatest(data?.owners ?? [])
    const norm = (uri: string) => (uri.endsWith('/') ? uri : uri + '/')
    const resolve = (uri: string): MarkState => {
      const p = norm(uri)
      let win: KeepRow | null = null
      let under = 0
      for (const r of keeps.values()) {
        if (p.startsWith(r.prefix)) {
          if (!win || newer(r, win)) win = r
        } else if (r.prefix.startsWith(p) && r.keep != null) under++
      }
      const mark = win?.keep != null
        ? { prefix: win.prefix, action: win.keep, who: win.who, ts: win.ts, note: win.memo }
        : null
      return { mark, own: mark != null && win!.prefix === p, under }
    }
    const claimOf = (uri: string): Owner | null => {
      const p = norm(uri)
      let win: OwnerRow | null = null
      for (const r of owners.values()) {
        if (p.startsWith(r.prefix) && (!win || newer(r, win))) win = r
      }
      return win?.owner != null ? { prefix: win.prefix, who: win.owner, ts: win.ts } : null
    }
    const overridesOf = (uri: string) => {
      const p = norm(uri)
      let n = 0
      let kept = 0
      for (const r of keeps.values()) {
        if (r.prefix !== p && r.prefix.startsWith(p) && r.keep != null) {
          n++
          if (r.keep !== 'sweep') kept++
        }
      }
      return { n, keeps: kept }
    }
    let count = 0
    for (const r of keeps.values()) if (r.keep != null) count++
    return { resolve, claimOf, overridesOf, count }
  }, [data])
}
