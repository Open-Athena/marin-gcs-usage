import { type ReactNode, useState } from 'react'
import { signInUrl, useCanMark } from './auth'
import type { Mark, MarkAction, MarkIndex } from './marks'
import { ACTION_LABELS, useMarkMutations } from './marks'
import { looksCkpt } from './sweep'
import { Tooltip } from './Tooltip'
import type { TreeNode } from './types'

// Marking UI for one prefix — rendered inside the pinned treemap tooltip and
// above the map for the current drill root. Shows the node's effective fate
// (most-recent-wins over ancestor marks) with provenance, and buttons to
// set/clear its mark. Sweep is the default fate, so "no mark" reads as
// "swept by default". Marking a prefix that has deeper marks inside repaints
// them (recency beats specificity) — hence the inline override confirm.

export const ACTION_COLORS: Record<MarkAction, string> = {
  keep: 'var(--mk-keep)',
  keep_last_ckpt: 'var(--mk-klc)',
  sweep: 'var(--mk-del)',
}

export const KLC_TIP =
  'Keep only the newest checkpoint under this prefix: the sweep deletes older step-/checkpoint-numbered dirs and keeps the highest step in each run. Offered on checkpoint-shaped directories.'

// Per-button tooltips. Nothing here deletes on click — "sweep" only *marks*
// for the mark-and-sweep campaign; removal happens after the deadline.
export const KEEP_TIP =
  'Keep this prefix — protect everything under it from the sweep. Takes no immediate action; nothing is deleted.'
export const SWEEP_TIP =
  'Mark this prefix for the sweep. Takes no immediate action — matching objects are deleted only after the sweep deadline.'
export const CLAIM_TIP =
  'Claim this prefix as yours — pulls it out of the unattributed “lost & found” so it shows up as your data.'
export const clearTip = (own: boolean): string =>
  own ? 'Remove your mark — back to unmarked (swept by default).'
      : 'Override the inherited mark: explicitly unmark this subtree.'

/** Short date for a mark's timestamp, e.g. "Aug 24, 2026". */
export const fmtMarkDate = (ts: number): string =>
  new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })

/** Tooltip body for a state chip: who set the mark, when, and whether inherited. */
export function markProvenance(mark: Mark, own: boolean): ReactNode {
  return (
    <>
      <b>{ACTION_LABELS[mark.action]}</b> by {mark.who} · {fmtMarkDate(mark.ts)}
      {!own && <> · inherited from <code>{mark.prefix}</code></>}
    </>
  )
}

/**
 * `node`: the tree node behind `uri`, when the caller has it — gates the
 * `keep_last_ckpt` button to checkpoint-shaped dirs. Omit (typed prefixes,
 * below the depth cap) and the button stays available.
 */
export function MarkControls({ uri, idx, node }: { uri: string; idx: MarkIndex; node?: TreeNode }) {
  const { put, claim } = useMarkMutations()
  const canMark = useCanMark()
  const [note, setNote] = useState('')
  const [pending, setPending] = useState<{ action: MarkAction | null } | null>(null)
  if (!uri.startsWith('gs://') || uri.indexOf('/', 5) === -1) {
    // store root ("gs://…" with no bucket path) — nothing markable
    return null
  }
  const prefix = uri.endsWith('/') ? uri : uri + '/'
  const { mark, own, under } = idx.resolve(uri)
  const cl = idx.claimOf(uri)
  const klcOk = node ? looksCkpt(node, uri) : true
  const ov = idx.overridesOf(uri)

  const write = (action: MarkAction | null) => {
    setPending(null)
    put.mutate({ prefix, action, note: note.trim() || undefined }, { onSuccess: () => setNote('') })
  }
  // Recency semantics: a mark here repaints every deeper mark in the subtree.
  // Confirm before doing that to a subtree someone already reviewed.
  const set = (action: MarkAction | null) => (ov.n > 0 ? setPending({ action }) : write(action))

  return (
    <div className="mark-controls" onClick={e => e.stopPropagation()}>
      <span className="fate">
        {mark ? (
          <>
            <span className="sw" style={{ background: ACTION_COLORS[mark.action] }} />
            <b>{ACTION_LABELS[mark.action]}</b>
            <span className="prov" title={own ? undefined : `inherited from ${mark.prefix}`}>
              {own ? '' : 'inherited · '}
              {mark.who}, {new Date(mark.ts * 1000).toLocaleDateString()}
              {mark.note ? ` — ${mark.note}` : ''}
            </span>
          </>
        ) : (
          <>
            <span className="sw" style={{ background: 'var(--mk-del)' }} />
            <b>unmarked</b>
            <span className="prov">deleted by default after the sweep deadline</span>
          </>
        )}
        {under > 0 && <span className="under">{under} mark{under === 1 ? '' : 's'} inside</span>}
        {cl && <span className="claimed">claimed by {cl.who}</span>}
      </span>
      {canMark ? (
        <span className="buttons">
          <Tooltip content={KEEP_TIP}>
            <button type="button" className={own && mark?.action === 'keep' ? 'on' : ''} onClick={() => set('keep')}>keep</button>
          </Tooltip>
          {klcOk && (
            <Tooltip content={KLC_TIP}>
              <button type="button" className={own && mark?.action === 'keep_last_ckpt' ? 'on' : ''} onClick={() => set('keep_last_ckpt')}>keep last ckpt</button>
            </Tooltip>
          )}
          <Tooltip content={SWEEP_TIP}>
            <button type="button" className={own && mark?.action === 'sweep' ? 'on' : ''} onClick={() => set('sweep')}>sweep</button>
          </Tooltip>
          {mark && (
            <Tooltip content={clearTip(own)}>
              <button type="button" onClick={() => set(null)}>clear</button>
            </Tooltip>
          )}
          {!cl && (
            <Tooltip content={CLAIM_TIP}>
              <button type="button" onClick={() => claim.mutate({ prefix })}>claim</button>
            </Tooltip>
          )}
          <input value={note} onChange={e => setNote(e.target.value)} placeholder="note (optional)" size={14} />
        </span>
      ) : (
        <span className="guest-note">
          viewing as guest — <a href={signInUrl()}>sign in</a> with your email to mark
        </span>
      )}
      {pending && (
        <span className="override-confirm">
          overrides <b>{ov.n}</b> more-specific mark{ov.n === 1 ? '' : 's'} inside
          {ov.keeps > 0 && <> (<b>{ov.keeps}</b> currently kept)</>} —
          <button type="button" onClick={() => write(pending.action)}>
            {pending.action === null ? 'clear' : ACTION_LABELS[pending.action]} anyway
          </button>
          <button type="button" onClick={() => setPending(null)}>cancel</button>
        </span>
      )}
      {(put.error ?? claim.error) && <span className="err">{(put.error ?? claim.error)!.message}</span>}
    </div>
  )
}
