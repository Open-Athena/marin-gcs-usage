import { type ReactNode, useState } from 'react'
import { Avatar } from './Avatar'
import { UserChip, allUsers, ghHandle, shortName } from './UserChip'
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
  'Claim this prefix — assign an owner (you by default, or pick someone). Pulls it out of the unattributed “Unclaimed” pool so it shows up as that person’s data.'
export const NOTE_TIP =
  'Optional memo stored on the keep/sweep/clear action you take next — a reason others (and future you) can see in the mark history. Not the same as the owner.'
export const clearTip = (own: boolean): string =>
  own ? 'Remove your mark — back to unmarked (swept by default).'
      : 'Override the inherited mark: explicitly unmark this subtree.'

/** Short date for a mark's timestamp, e.g. "Aug 24, 2026". */
export const fmtMarkDate = (ts: number): string =>
  new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })

/** Tooltip body for a state chip: who set the mark, when, and whether inherited. */
export function markProvenance(mark: Mark, own: boolean): ReactNode {
  return (
    <span className="mark-prov">
      <span className="sw" style={{ background: ACTION_COLORS[mark.action] }} />
      <Avatar github={ghHandle(mark.who)} name={shortName(mark.who)} size={16} />
      <span>
        <b>{ACTION_LABELS[mark.action]}</b> by {shortName(mark.who)} · {fmtMarkDate(mark.ts)}
        {!own && <> · inherited from <code>{mark.prefix}</code></>}
      </span>
    </span>
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
  const [assign, setAssign] = useState('')
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

  // Assign owner: a typed short name resolves to its canonical id; empty = the
  // signed-in actor (`@me`); an unknown string passes through (id / email).
  const doClaim = () => {
    const v = assign.trim()
    const owner = v ? (allUsers().find(u => u.name.toLowerCase() === v.toLowerCase())?.id ?? v) : undefined
    claim.mutate({ prefix, owner })
    setAssign('')
  }

  // The set-decision buttons, colored by fate. `keep_last_ckpt` only on
  // checkpoint-shaped dirs.
  const actions: { a: MarkAction; label: string; tip: string }[] = [
    { a: 'keep', label: 'keep', tip: KEEP_TIP },
    ...(klcOk ? [{ a: 'keep_last_ckpt' as const, label: 'keep last ckpt', tip: KLC_TIP }] : []),
    { a: 'sweep', label: 'sweep', tip: SWEEP_TIP },
  ]

  return (
    <div className="mark-controls" onClick={e => e.stopPropagation()}>
      {/* Status — the current decision and who set it (read-only provenance). */}
      <span className="fate">
        <span className="chip" style={{ background: mark ? ACTION_COLORS[mark.action] : 'var(--mk-del)' }}>
          {mark?.action === 'keep' ? '✓' : mark?.action === 'keep_last_ckpt' ? '◐' : '✕'}
        </span>
        <b>{mark ? ACTION_LABELS[mark.action] : 'unmarked'}</b>
        {mark ? (
          <span className="prov" title={own ? undefined : `inherited from ${mark.prefix}`}>
            {own ? 'set by' : 'inherited ·'}{' '}
            <Avatar github={ghHandle(mark.who)} name={shortName(mark.who)} size={14} /> {shortName(mark.who)}
            {' · '}{new Date(mark.ts * 1000).toLocaleDateString()}
            {mark.note ? ` — ${mark.note}` : ''}
          </span>
        ) : (
          <span className="prov">swept by default once the review window closes</span>
        )}
        {under > 0 && <span className="under">{under} mark{under === 1 ? '' : 's'} inside</span>}
      </span>
      {canMark ? (
        <>
          {/* Set the decision — colored, active one filled. */}
          <span className="buttons">
            {actions.map(({ a, label, tip }) => (
              <Tooltip key={a} content={tip}>
                <button type="button" className={`act ${a}${own && mark?.action === a ? ' on' : ''}`} onClick={() => set(a)}>{label}</button>
              </Tooltip>
            ))}
            {mark && (
              <Tooltip content={clearTip(own)}>
                <button type="button" className="act clear" onClick={() => set(null)}>clear</button>
              </Tooltip>
            )}
            <Tooltip content={NOTE_TIP}>
              <input className="note" value={note} onChange={e => setNote(e.target.value)} placeholder="note on this mark (optional)" size={20} />
            </Tooltip>
          </span>
          {/* Owner — claim for yourself, or assign to anyone (avatar + name). */}
          <span className="owner">
            <span className="lbl">owner</span>
            {cl ? <UserChip who={cl.who} size={15} /> : <span className="none">unclaimed</span>}
            <input
              list="mk-assign-users" className="assign" value={assign}
              onChange={e => setAssign(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') doClaim() }}
              placeholder={cl ? 'reassign to…' : 'you'} size={12} spellCheck={false}
            />
            <datalist id="mk-assign-users">{allUsers().map(u => <option key={u.id} value={u.name} />)}</datalist>
            <Tooltip content={CLAIM_TIP}>
              <button type="button" onClick={doClaim}>{cl ? 'reassign' : 'claim'}</button>
            </Tooltip>
            {cl && <button type="button" onClick={() => claim.mutate({ prefix, release: true })}>release</button>}
          </span>
        </>
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
