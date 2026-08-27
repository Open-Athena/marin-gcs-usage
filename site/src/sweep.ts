// Tree walks behind the /mark tabs: collect the maximal subtrees that a
// review lens cares about (mine / unattributed / communal), so each tab is a
// ranked worklist of prefixes rather than a hunt through the treemap.
import { useQuery } from '@tanstack/react-query'
import { reaggregate, type NodePred } from './filterTree'
import { newer, type KeepRow, type MarkAction, type MarkIndex, type OwnerRow } from './marks'
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

// ---- Fast fate walks -------------------------------------------------------
// `MarkIndex.resolve` is O(marked prefixes) per call — fine per rendered cell,
// quadratic-feeling over a whole-tree walk. These walkers instead thread the
// winning row down the DFS (newest ancestor-or-equal row, same semantics as
// `resolve`) and answer "any live mark strictly below?" from a precomputed
// ancestor set, so each node costs O(1).

export type Fate = MarkAction | 'unmarked'

interface FateWalkCtx {
  /** Latest live row exactly on this (trailing-`/`) prefix. */
  own: (uri: string) => KeepRow | undefined
  /** Latest live claim exactly on this prefix (when claims are threaded). */
  ownOwner: (uri: string) => OwnerRow | undefined
  /** Any live set-mark or claim strictly below this prefix? */
  below: (uri: string) => boolean
}

function fateWalkCtx(keeps: Map<string, KeepRow>, owners?: Map<string, OwnerRow>): FateWalkCtx {
  const anc = new Set<string>()
  const addAnc = (prefix: string) => {
    // gs://bucket/a/b/ → ancestors gs://bucket/, gs://bucket/a/
    const segs = prefix.replace(/\/+$/, '').split('/')
    for (let i = 3; i < segs.length; i++) anc.add(segs.slice(0, i).join('/') + '/')
  }
  for (const r of keeps.values()) if (r.keep != null) addAnc(r.prefix)
  // Claims count as "something below" too — the walk must descend far enough
  // to apply ownership overrides at their prefixes.
  if (owners) for (const r of owners.values()) if (r.owner != null) addAnc(r.prefix)
  const norm = (uri: string) => (uri.endsWith('/') ? uri : uri + '/')
  return {
    own: uri => keeps.get(norm(uri)),
    ownOwner: uri => owners?.get(norm(uri)),
    below: uri => anc.has(norm(uri)),
  }
}

/** Newest of the inherited claim and this prefix's own (a newer null-owner
 * row releases inherited claims). */
const winOwner = (ctx: FateWalkCtx, uri: string, inherited: OwnerRow | null): OwnerRow | null => {
  const own = ctx.ownOwner(uri)
  return own && (!inherited || newer(own, inherited)) ? own : inherited
}

/** Newest of the inherited winner and this prefix's own row (clears count:
 * a newer `keep: null` row repaints inherited marks back to unmarked). */
const winRow = (ctx: FateWalkCtx, uri: string, inherited: KeepRow | null): KeepRow | null => {
  const own = ctx.own(uri)
  return own && (!inherited || newer(own, inherited)) ? own : inherited
}

const fateOf = (win: KeepRow | null): Fate => win?.keep ?? 'unmarked'

// ---- keep_last_ckpt decomposition -----------------------------------------
// A KLC mark means "within each checkpoint run under this prefix, keep the
// newest step dir; sweep the rest". Aggregations shouldn't show that as its
// own category — they should show the *actual* keep/sweep proportions.

const CKPT_NUM_RE = /^(?:step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?(\d+)/i
const normUri = (uri: string): string => (uri.endsWith('/') ? uri : uri + '/')

export interface KlcSplit {
  /** Kept subtrees (each ckpt-parent's newest step child), with their bytes. */
  kept: { uri: string; b: number }[]
  keptB: number
  totalB: number
}

export type KlcIndex = Map<string, KlcSplit>

/**
 * For each live `keep_last_ckpt` mark, walk its subtree in the scan tree: at
 * every node with step-numbered children, the max-step child is kept (no
 * deeper recursion); everything else sweeps. Marks whose prefix the tree
 * can't resolve, or with no ckpt-shaped descendants in view, get no entry —
 * callers render those as first-class KLC (amber).
 */
export function klcSplits(root: TreeNode, keeps: Map<string, KeepRow>): KlcIndex {
  const out: KlcIndex = new Map()
  for (const r of keeps.values()) {
    if (r.keep !== 'keep_last_ckpt') continue
    const p = normUri(r.prefix)
    let node: TreeNode | undefined = root
    for (const s of p.replace(/^[a-z0-9]+:\/\//, '').replace(/\/+$/, '').split('/')) {
      node = node?.c?.find(c => c.n === s)
    }
    if (!node) continue
    const kept: { uri: string; b: number }[] = []
    const walk = (n: TreeNode, u: string) => {
      const steps = (n.c ?? [])
        .map(c => ({ c, m: CKPT_NUM_RE.exec(c.n) }))
        .filter((x): x is { c: TreeNode; m: RegExpExecArray } => x.m != null)
      if (steps.length) {
        let best = steps[0]
        for (const s of steps) if (Number(s.m[1]) > Number(best.m[1])) best = s
        kept.push({ uri: `${u}${best.c.n}/`, b: best.c.b })
        return
      }
      for (const c of n.c ?? []) if (!c.n.startsWith('(')) walk(c, `${u}${c.n}/`)
    }
    walk(node, p)
    if (kept.length) out.set(p, { kept, keptB: kept.reduce((s, k) => s + k.b, 0), totalB: node.b })
  }
  return out
}

/** A klc-governed uri's concrete fate: inside a kept subtree → keep; contains
 * kept subtrees → mixed (caller splits by `klcKeptWithin`); else sweep. */
export const klcFateAt = (uri: string, split: KlcSplit): 'keep' | 'sweep' | 'mixed' => {
  const u = normUri(uri)
  if (split.kept.some(k => u.startsWith(k.uri))) return 'keep'
  if (split.kept.some(k => k.uri.startsWith(u))) return 'mixed'
  return 'sweep'
}

/** Kept bytes inside `uri` (for proportional splits at mixed nodes). */
export const klcKeptWithin = (uri: string, split: KlcSplit): number => {
  const u = normUri(uri)
  return split.kept.reduce((s, k) => s + (k.uri.startsWith(u) ? k.b : 0), 0)
}

/**
 * Scope the map to the undecided estate (the To-do lens): prune any subtree
 * covered by a keep/sweep decision, keep fully-clean subtrees whole, recurse
 * into mixed ones and re-aggregate ancestors. Folded `(other)` tiles inside
 * mixed nodes are dropped — the tree can't say what's inside them.
 */
export function applyTodoFilter(root: TreeNode, idx: MarkIndex): TreeNode {
  const ctx = fateWalkCtx(idx.keeps)
  const walk = (n: TreeNode, uri: string, inherited: KeepRow | null): TreeNode | null => {
    const win = winRow(ctx, uri, inherited)
    if (!ctx.below(uri)) return fateOf(win) === 'unmarked' ? n : null
    const kids = (n.c ?? [])
      .filter(c => !c.n.startsWith('('))
      .map(c => walk(c, `${uri}/${c.n}`, win))
      .filter((c): c is TreeNode => c != null)
    return kids.length ? reaggregate(n, kids) : null
  }
  const buckets = (root.c ?? [])
    .map(b => walk(b, `gs://${b.n}`, null))
    .filter((c): c is TreeNode => c != null)
  return reaggregate(root, buckets)
}

/**
 * Per-user bytes by fate across the whole tree, in one walk: descend only
 * while a subtree still holds deeper marks; at each settle point distribute
 * the node's `us` shares (minus what descended into recursed children — so
 * folded tiles and floor residue take the node's own fate).
 */
export function allUserFates(
  root: TreeNode,
  idx: MarkIndex,
  klc?: KlcIndex,
  /** Canonicalize claim `owner` values (emails → user ids); claims are the
   * ownership WAL — a claimed subtree attributes wholly to its claimant,
   * overriding scan attribution until the pipeline catches up. */
  canon: (who: string) => string = w => w,
): Map<string, Record<Fate, number>> {
  const ctx = fateWalkCtx(idx.keeps, idx.owners)
  const out = new Map<string, Record<Fate, number>>()
  const add = (u: string, f: Fate, b: number) => {
    let rec = out.get(u)
    if (!rec) out.set(u, (rec = { keep: 0, keep_last_ckpt: 0, sweep: 0, unmarked: 0 }))
    rec[f] += b
  }
  // Settle `each(f, frac)` bytes at `uri` under `win` — KLC decomposes into
  // its real keep/sweep proportions when the split is resolvable.
  const settle = (uri: string, nodeB: number, win: KeepRow | null, each: (f: Fate, frac: number) => void) => {
    const f = fateOf(win)
    if (f !== 'keep_last_ckpt' || !klc) return each(f, 1)
    const split = klc.get(win!.prefix.endsWith('/') ? win!.prefix : win!.prefix + '/')
    if (!split) return each(f, 1)
    const rel = klcFateAt(uri, split)
    if (rel !== 'mixed') return each(rel, 1)
    const ratio = nodeB > 0 ? Math.min(1, klcKeptWithin(uri, split) / nodeB) : 0
    each('keep', ratio)
    each('sweep', 1 - ratio)
  }
  const walk = (n: TreeNode, uri: string, inhKeep: KeepRow | null, inhOwn: OwnerRow | null) => {
    const win = winRow(ctx, uri, inhKeep)
    const ownRow = winOwner(ctx, uri, inhOwn)
    const claimant = ownRow?.owner != null ? canon(ownRow.owner) : null
    if (!ctx.below(uri)) {
      settle(uri, n.b, win, (f, frac) => {
        if (claimant) add(claimant, f, n.b * frac)
        else for (const [u, b] of n.us ?? []) if (b > 0) add(u, f, b * frac)
      })
      return
    }
    const rest = new Map<string, number>(n.us ?? [])
    let restB = n.b
    for (const c of n.c ?? []) {
      if (c.n.startsWith('(')) continue
      restB -= c.b
      walk(c, `${uri}/${c.n}`, win, ownRow)
      for (const [u, b] of c.us ?? []) rest.set(u, (rest.get(u) ?? 0) - b)
    }
    settle(uri, n.b, win, (f, frac) => {
      if (claimant) { if (restB > 0) add(claimant, f, restB * frac) }
      else for (const [u, b] of rest) if (b > 0) add(u, f, b * frac)
    })
  }
  for (const b of root.c ?? []) walk(b, `gs://${b.n}`, null, null)
  return out
}

/**
 * Fate totals (bytes) for one subtree — the "of the current view, how much is
 * keep / sweep / undecided" rollup. `uri` is the node's full URI (`''` for
 * the artifact root, whose children are buckets); marks inherited from
 * ancestors of `uri` are folded in. KLC decomposes via `klc` when given —
 * bytes under an unresolvable KLC mark stay in `keep_last_ckpt`.
 */
export function subtreeFateTotals(
  node: TreeNode,
  uri: string,
  idx: MarkIndex,
  klc?: KlcIndex,
): Record<Fate, number> {
  const ctx = fateWalkCtx(idx.keeps)
  const out: Record<Fate, number> = { keep: 0, keep_last_ckpt: 0, sweep: 0, unmarked: 0 }
  const settle = (u: string, b: number, win: KeepRow | null) => {
    if (b <= 0) return
    const f = fateOf(win)
    if (f !== 'keep_last_ckpt' || !klc) { out[f] += b; return }
    const split = klc.get(win!.prefix.endsWith('/') ? win!.prefix : win!.prefix + '/')
    if (!split) { out[f] += b; return }
    const rel = klcFateAt(u, split)
    if (rel === 'keep') out.keep += b
    else if (rel === 'sweep') out.sweep += b
    else {
      const kept = Math.min(b, klcKeptWithin(u, split))
      out.keep += kept
      out.sweep += b - kept
    }
  }
  const walk = (n: TreeNode, u: string, inherited: KeepRow | null) => {
    const win = winRow(ctx, u, inherited)
    if (!ctx.below(u)) return settle(u, n.b, win)
    let rest = n.b
    for (const c of n.c ?? []) {
      if (c.n.startsWith('(')) continue
      rest -= c.b
      walk(c, `${u}/${c.n}`, win)
    }
    settle(u, rest, win)
  }
  if (uri === '') {
    for (const b of node.c ?? []) walk(b, `gs://${b.n}`, null)
    return out
  }
  // Fold in marks on ancestors of `uri` (the drilled node inherits them).
  const clean = uri.replace(/^([a-z0-9]+:\/\/)/, '')
  const scheme = uri.slice(0, uri.length - clean.length) || 'gs://'
  const segs = clean.replace(/\/+$/, '').split('/')
  let inherited: KeepRow | null = null
  for (let i = 1; i < segs.length; i++) {
    inherited = winRow(ctx, `${scheme}${segs.slice(0, i).join('/')}`, inherited)
  }
  walk(node, `${scheme}${segs.join('/')}`, inherited)
  return out
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
