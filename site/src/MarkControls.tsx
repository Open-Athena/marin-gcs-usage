import { useState } from 'react'
import { signInUrl, useCanMark } from './auth'
import type { MarkAction, MarkIndex } from './marks'
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
          <button type="button" className={own && mark?.action === 'keep' ? 'on' : ''} onClick={() => set('keep')}>keep</button>
          {klcOk && (
            <Tooltip content={KLC_TIP}>
              <button type="button" className={own && mark?.action === 'keep_last_ckpt' ? 'on' : ''} onClick={() => set('keep_last_ckpt')}>keep last ckpt</button>
            </Tooltip>
          )}
          <button type="button" className={own && mark?.action === 'sweep' ? 'on' : ''} onClick={() => set('sweep')}>delete</button>
          {mark && <button type="button" title={own ? 'remove this mark (back to swept-by-default)' : 'override the inherited mark: explicitly unmark this subtree'} onClick={() => set(null)}>clear</button>}
          {!cl && <button type="button" title="claim this prefix as yours (lost & found)" onClick={() => claim.mutate({ prefix })}>claim</button>}
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
