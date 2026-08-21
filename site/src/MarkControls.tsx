import { useState } from 'react'
import type { MarkAction, MarkIndex } from './marks'
import { ACTION_LABELS, useMarkMutations } from './marks'

// Marking UI for one prefix — rendered inside the pinned treemap tooltip and
// above the map for the current drill root. Shows the node's effective fate
// (deepest-mark-wins) with provenance, and buttons to set/clear its own mark.
// `delete` is the default fate, so "no mark" reads as "swept by default".

export const ACTION_COLORS: Record<MarkAction, string> = {
  keep: 'var(--mk-keep)',
  keep_last_ckpt: 'var(--mk-klc)',
  delete: 'var(--mk-del)',
}

export function MarkControls({ uri, idx }: { uri: string; idx: MarkIndex }) {
  const { put, claim } = useMarkMutations()
  const [note, setNote] = useState('')
  if (!uri.startsWith('gs://') || uri.indexOf('/', 5) === -1) {
    // store root ("gs://…" with no bucket path) — nothing markable
    return null
  }
  const prefix = uri.endsWith('/') ? uri : uri + '/'
  const { mark, own, under } = idx.resolve(uri)
  const cl = idx.claimOf(uri)

  const set = (action: MarkAction | null) =>
    put.mutate({ prefix, action, note: note.trim() || undefined }, { onSuccess: () => setNote('') })

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
      <span className="buttons">
        <button type="button" className={own && mark?.action === 'keep' ? 'on' : ''} onClick={() => set('keep')}>keep</button>
        <button type="button" className={own && mark?.action === 'keep_last_ckpt' ? 'on' : ''} onClick={() => set('keep_last_ckpt')}>keep last ckpt</button>
        <button type="button" className={own && mark?.action === 'delete' ? 'on' : ''} onClick={() => set('delete')}>delete</button>
        {own && <button type="button" onClick={() => set(null)}>clear</button>}
        {!cl && <button type="button" title="claim this prefix as yours (lost & found)" onClick={() => claim.mutate({ prefix })}>claim</button>}
        <input value={note} onChange={e => setNote(e.target.value)} placeholder="note (optional)" size={14} />
      </span>
      {(put.error ?? claim.error) && <span className="err">{(put.error ?? claim.error)!.message}</span>}
    </div>
  )
}
