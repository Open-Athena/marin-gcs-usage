// One action on a filter's matches (a table row's trash / assign, the selection bar's, the bulk bar's), as a
// small state machine with no DOM: offered at once (no "list" step first), the matches resolved on the
// click (the hover usually started that fetch), then confirmed if large, sent in batches with progress, and
// accepted — or, when they can't be acted on here (over the cap, a refused query), a muted note in place
// of the control. Only a 5xx or a network failure is red.
import type { CoverItem } from '../functions/_lib/cover'
import { ASSIGN_CHUNK, assignInBatches, chunks, isBucketPrefix, isRefusal, type OwnerPost, ownerPost, STAGE_CHUNK, stageMany, unstageMany } from './batches'
import { actionTargets, type Resolved, type Targets, targetsText } from './coverTargets'

export type ActKind = 'assign' | 'stage'

/** What the viewer asked for: assign (to `owner`, `'@me'` by default; null unassigns) or stage. */
export interface ActReq {
  kind: ActKind
  owner?: string | null
  /** The assignee as the viewer named them (`you`, `nobody`, a name): the accepted state says it. */
  who?: string
  /** The owner action's memo / the staging batch's note. */
  memo?: string
}

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

/** What an action says about the items it leaves out, in plain words (one sentence per caveat). */
export function caveats(t: { buckets: number; unknown: number }, action: ActKind): string[] {
  const out: string[] = []
  if (action === 'stage' && t.buckets) out.push(`${plural(t.buckets, 'whole bucket')} can’t be staged; ${t.buckets === 1 ? 'it is' : 'they are'} left out.`)
  if (t.unknown) out.push(`${plural(t.unknown, 'match', 'matches')} couldn’t be checked (file or folder?) and ${t.unknown === 1 ? 'is' : 'are'} left out; open ${t.unknown === 1 ? 'its' : 'their'} folder to act on ${t.unknown === 1 ? 'it' : 'them'}.`)
  return out
}

/** Past this many items an action asks first (the bulk bar always asks: it has the review list; a table row
 *  asks for anything but the one item its label names, `ActDeps.shown`). */
export const CONFIRM_OVER = 50

export type ActState =
  | { s: 'idle' }
  /** Listing the matches (`/api/filter-cover`, or the cached slice): a spinner. */
  | { s: 'resolving'; req: ActReq }
  /** Large, a whole bucket's assignment, or a row's items not just the one its label names: what it will
   *  send, and a confirm. */
  | { s: 'confirm'; req: ActReq; got: Resolved }
  /** Sending: `batch` of `batches` POSTs done (a bar when there are several, else a spinner). */
  | { s: 'sending'; req: ActReq; t: Targets; batch: number; batches: number }
  /** Accepted: what landed, and how to take it back (a staging that absorbed nothing). */
  | { s: 'done'; req: ActReq; text: string; undo?: { plan_id: number; keys: string[] } }
  | { s: 'undoing'; req: ActReq }
  /** Not acted on, by design (over the cap, not all listable, a refusal): the reason, muted. */
  | { s: 'muted'; reason: string }
  /** A 5xx or the network: red, with a retry. */
  | { s: 'failed'; req: ActReq; msg: string }

export interface ActDeps {
  /** The matches the action takes (`rowCover`, the view's cover, …). */
  resolve: () => Promise<Resolved>
  /** The items kept from what resolved (the bulk bar's unticked ones dropped). Default: all. */
  pick?: (got: Resolved) => CoverItem[]
  scheme: string
  /** One `/api/actions` POST (≤ `ASSIGN_CHUNK` actions). */
  assign: (actions: OwnerPost[]) => Promise<unknown>
  /** The plan API (`plans.ts` `call`): stage, merge, unstage. */
  call: <T>(url: string, method: string, body: unknown) => Promise<T>
  /** The scan a staging is "as of". */
  asOf?: () => string | null | undefined
  /** Ask before every send, not just large ones. */
  alwaysConfirm?: boolean
  /** A table row's action: the path its label names (below the store root). Anything but exactly one item at
   *  or under it asks first, however small — the label must never under-state what a click sends. */
  shown?: string
  /** After anything landed (refresh what shows it). */
  landed?: (kind: ActKind) => void
}

/** The targets an action takes from what resolved. */
export const targetsOf = (got: Resolved, req: ActReq, d: Pick<ActDeps, 'pick' | 'scheme'>): Targets =>
  actionTargets(d.pick ? d.pick(got) : got.items, d.scheme, req.kind)

/** Whole buckets among an assignment's targets: the newest action on an ancestor overrides everything under
 *  it, so one is never assigned without asking. */
export const bucketsIn = (t: Targets): string[] => t.items.filter(i => i.kind === 'prefix' && isBucketPrefix(i.key)).map(i => i.key)

/** Whether a row's targets are exactly what its label names: one item, at or under `shown` (its key below
 *  `scheme`, a folder's trailing `/` dropped). */
export function withinShown(t: Pick<Targets, 'items'>, shown: string, scheme: string): boolean {
  if (t.items.length !== 1) return false
  const p = t.items[0].key.slice(scheme.length).replace(/\/$/, '')
  return p === shown || p.startsWith(`${shown}/`)
}

/** Why nothing would be sent, in words (every match unchecked, or only whole buckets to stage). */
export function nothingReason(t: Targets, kind: ActKind): string {
  if (t.unknown && !t.buckets) return 'These matches couldn’t be checked (file or folder?); open the folder to act on them.'
  if (t.buckets && kind === 'stage') return 'Whole buckets can’t be staged for deletion; open the bucket to act on its matches.'
  return 'Nothing here to act on.'
}

/** What follows a resolve: muted (not all listed, or nothing to send), a confirm, or straight to sending. */
export function afterResolve(got: Resolved, req: ActReq, d: Pick<ActDeps, 'pick' | 'scheme' | 'alwaysConfirm' | 'shown'>): ActState | { s: 'go'; t: Targets } {
  if (!got.complete) return { s: 'muted', reason: got.reason ?? 'These matches can’t all be listed here; open a folder below to act on its matches.' }
  const t = targetsOf(got, req, d)
  if (!t.items.length) return { s: 'muted', reason: nothingReason(t, req.kind) }
  if (d.alwaysConfirm || t.items.length > CONFIRM_OVER || (req.kind === 'assign' && bucketsIn(t).length)) return { s: 'confirm', req, got }
  if (d.shown != null && !withinShown(t, d.shown, d.scheme)) return { s: 'confirm', req, got }
  return { s: 'go', t }
}

/** A failure's state: a refusal (4xx) muted, anything else red. */
export const failState = (e: unknown, req: ActReq): ActState =>
  isRefusal(e) ? { s: 'muted', reason: (e as Error).message } : { s: 'failed', req, msg: e instanceof Error ? e.message : String(e) }

const chunkOf = (k: ActKind) => (k === 'stage' ? STAGE_CHUNK : ASSIGN_CHUNK)

/** The controller over a state cell (`get` / `set`): `start` on a click, `confirm` / `cancel` the large-set
 *  question, `undo` an accepted staging, `retry` a failure, `dismiss` back to idle. Each step checks it is
 *  still current (a cancel or dismiss mid-resolve drops the late answer). */
export function actController(deps: () => ActDeps, set: (s: ActState) => void, get: () => ActState) {
  const send = async (req: ActReq, t: Targets) => {
    const d = deps()
    const batches = chunks(t.items, chunkOf(req.kind)).length
    let cur: ActState = { s: 'sending', req, t, batch: 0, batches }
    set(cur)
    const progress = (done: number) => { cur = { ...cur, batch: Math.ceil(done / chunkOf(req.kind)) } as ActState; set(cur) }
    try {
      if (req.kind === 'assign') {
        await assignInBatches(t.items, i => ownerPost(i, req.owner === undefined ? '@me' : req.owner, req.memo), d.assign, progress)
        set({ s: 'done', req, text: `${req.owner === null ? 'unassigned' : 'assigned'} ${targetsText(t)}${req.owner === null ? '' : ` → ${req.who ?? 'you'}`}` })
      } else {
        const r = await stageMany(t.items, req.memo, d.asOf?.(), d.call, progress)
        const files = r.staged_objects?.length ?? 0
        const text = r.staged.length ? `staged ${targetsText({ folders: r.staged.length - files, files })}` : 'already staged'
        set({ s: 'done', req, text, ...(r.staged.length && !r.absorbed.length && r.plan_id != null ? { undo: { plan_id: r.plan_id, keys: r.staged } } : {}) })
      }
      d.landed?.(req.kind)
    } catch (e) {
      set(failState(e, req))
    }
  }
  const start = async (req: ActReq) => {
    const s0 = get().s
    if (s0 === 'resolving' || s0 === 'sending' || s0 === 'undoing') return
    set({ s: 'resolving', req })
    let got: Resolved
    try {
      got = await deps().resolve()
    } catch (e) {
      if (get().s === 'resolving') set(failState(e, req))
      return
    }
    const cur = get()
    if (cur.s !== 'resolving' || cur.req !== req) return
    const next = afterResolve(got, req, deps())
    if (next.s === 'go') await send(req, next.t)
    else set(next)
  }
  const confirm = async () => {
    const cur = get()
    if (cur.s !== 'confirm') return
    const t = targetsOf(cur.got, cur.req, deps())
    if (!t.items.length) { set({ s: 'muted', reason: nothingReason(t, cur.req.kind) }); return }
    await send(cur.req, t)
  }
  const undo = async () => {
    const cur = get()
    if (cur.s !== 'done' || !cur.undo) return
    const d = deps()
    set({ s: 'undoing', req: cur.req })
    try {
      const n = await unstageMany(cur.undo.plan_id, cur.undo.keys, d.call)
      set({ s: 'done', req: cur.req, text: `unstaged ${n.toLocaleString('en-US')} ${n === 1 ? 'item' : 'items'}` })
      d.landed?.(cur.req.kind)
    } catch (e) {
      set(failState(e, cur.req))
    }
  }
  const cancel = () => { if (get().s === 'confirm' || get().s === 'resolving') set({ s: 'idle' }) }
  const dismiss = () => { const s = get().s; if (s === 'done' || s === 'muted' || s === 'failed') set({ s: 'idle' }) }
  const retry = () => { const cur = get(); if (cur.s === 'failed') { set({ s: 'idle' }); return start(cur.req) } }
  return { start, confirm, cancel, undo, dismiss, retry }
}
export type ActController = ReturnType<typeof actController>
