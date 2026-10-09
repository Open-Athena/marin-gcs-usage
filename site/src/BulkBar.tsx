// Bulk actions over the filter's matches (`gcs:specs/done/selection-actions.md`): assign an owner to, or
// stage for deletion, every match under the view — as the fewest exact prefixes (`/api/filter-cover`: a
// folder every object of which matches stands for its matches), listed by parent folder so false
// positives can be dropped (a whole folder's worth, or one by one) before anything is sent. Large sets go
// in batches (`assignInBatches` / `stageInBatches`), never a refusal.
import { useEffect, useMemo, useState } from 'react'
import { type OwnerPost, useOwnerMutations } from './owners'
import { useStage } from './plans'
import { allUsers } from './UserChip'
import { useUnits } from './units'
import { actionTargets, ASSIGN_CHUNK, assignInBatches, chunks, type CoverItem, type FilterCover, groupItems, STAGE_CHUNK } from './filterCover'

/** Items listed per folder in the review panel (the folder's checkbox still takes or drops all of them). */
export const LIST_PER_GROUP = 100

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`

interface Pending { kind: 'assign' | 'stage'; label: string; owner?: string }

/** The item paths left out by the viewer: per item, or per folder (every item in it). */
export function useDropped(cover: FilterCover | undefined) {
  const [dropped, setDropped] = useState<ReadonlySet<string>>(new Set())
  useEffect(() => setDropped(new Set()), [cover])
  const toggle = (paths: string[], drop: boolean) => setDropped(d => applyToggle(d, paths, drop))
  return { dropped, toggle }
}

/** `dropped` with `paths` dropped (`drop`) or taken back. */
export function applyToggle(dropped: ReadonlySet<string>, paths: readonly string[], drop: boolean): ReadonlySet<string> {
  const n = new Set(dropped)
  for (const p of paths) if (drop) n.add(p); else n.delete(p)
  return n
}

/** What the bar says about the kept items for each action, in plain words (one sentence per caveat). */
export function caveats(t: { files: number; buckets: number; unknown: number }, action: 'assign' | 'stage'): string[] {
  const out: string[] = []
  if (action === 'assign' && t.files) out.push(`${plural(t.files, 'matching file')} ${t.files === 1 ? 'sits' : 'sit'} in folders that also hold files that don’t match. Owners are set per folder, so assigning leaves ${t.files === 1 ? 'it' : 'them'} out.`)
  if (action === 'stage' && t.buckets) out.push(`${plural(t.buckets, 'whole bucket')} can’t be staged; ${t.buckets === 1 ? 'it is' : 'they are'} left out.`)
  if (t.unknown) out.push(`${plural(t.unknown, 'match', 'matches')} couldn’t be checked (file or folder?) and ${t.unknown === 1 ? 'is' : 'are'} left out; open ${t.unknown === 1 ? 'its' : 'their'} folder to act on ${t.unknown === 1 ? 'it' : 'them'}.`)
  return out
}

export function BulkBar({ cover, loading, error, scheme, query, canAssign, canStage }: {
  cover?: FilterCover
  loading?: boolean
  error?: string | null
  scheme: string
  query: string
  canAssign: boolean
  canStage: boolean
}) {
  const { post } = useOwnerMutations()
  const stage = useStage()
  const { fmtBytes } = useUnits()
  const { dropped, toggle } = useDropped(cover)
  const [pending, setPending] = useState<Pending | null>(null)
  const [assign, setAssign] = useState('')
  const [memo, setMemo] = useState('')
  const [progress, setProgress] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const items = useMemo(() => cover?.items ?? [], [cover])
  const kept = useMemo(() => items.filter(i => !dropped.has(i.path)), [items, dropped])
  const groups = useMemo(() => groupItems(items), [items])
  const assignT = useMemo(() => actionTargets(kept, scheme, 'assign'), [kept, scheme])
  const stageT = useMemo(() => actionTargets(kept, scheme, 'stage'), [kept, scheme])

  if (!canAssign && !canStage) return null
  if (loading) return <span className="bulkbar"><span className="bb-scope">listing the matches…</span></span>
  if (error) return <span className="bulkbar"><span className="bb-warn">Can’t act on the matches: {error}</span></span>
  if (!cover || !cover.roots.n) return null
  const keptB = kept.reduce((n, i) => n + i.b, 0)
  if (!cover.complete) {
    return <span className="bulkbar"><span className="bb-scope">{plural(cover.roots.n, 'match', 'matches')}:</span> <span className="bb-warn">{cover.reason}</span></span>
  }
  const busy = progress != null
  const note = `bulk filter:'${query}'${memo ? ` — ${memo}` : ''}`

  const run = async (p: Pending) => {
    setDone(null)
    try {
      if (p.kind === 'assign') {
        const n = await assignInBatches(assignT.prefixes, (pattern): OwnerPost => ({ pattern, owner: p.owner ?? '@me', memo: note }), a => post.mutateAsync(a),
          (d, t) => setProgress(`${p.label}: ${d.toLocaleString('en-US')}/${t.toLocaleString('en-US')}…`))
        setDone(`assigned ${plural(n, 'prefix', 'prefixes')}`)
      } else {
        setProgress(`${p.label}: ${stageT.prefixes.length.toLocaleString('en-US')}…`)
        const r = await stage.mutateAsync({ prefixes: stageT.prefixes, note: memo })
        setDone(`staged ${plural(r.staged.length, 'prefix', 'prefixes')} for deletion (plan ${r.plan_id})`)
      }
      setPending(null)
      setMemo('')
    } finally {
      setProgress(null)
    }
  }
  const t = pending?.kind === 'stage' ? stageT : assignT
  const batches = pending ? chunks(t.prefixes, pending.kind === 'stage' ? STAGE_CHUNK : ASSIGN_CHUNK).length : 0

  return (
    <span className="bulkbar">
      <span className="bb-scope">
        {plural(cover.roots.n, 'match', 'matches')} → {kept.length === items.length ? plural(items.length, 'prefix', 'prefixes') : `${kept.length.toLocaleString('en-US')} of ${plural(items.length, 'prefix', 'prefixes')}`} · {fmtBytes(keptB)}
      </span>
      <details className="bb-review">
        <summary>review</summary>
        <div className="bb-panel" role="group" aria-label="Matches to act on">
          <p className="bb-help">Each line is a folder whose every file matches, or a single match. Untick false positives — a whole folder’s worth, or one at a time.</p>
          <div className="bb-bulk">
            <button type="button" className="act" onClick={() => toggle(items.map(i => i.path), false)}>all</button>
            <button type="button" className="act" onClick={() => toggle(items.map(i => i.path), true)}>none</button>
          </div>
          <ul className="bb-groups">
            {groups.map(g => <ReviewGroup key={g.parent} parent={g.parent} items={g.items} b={g.b} dropped={dropped} toggle={toggle} fmtBytes={fmtBytes} />)}
          </ul>
        </div>
      </details>
      {canAssign && <>
        <button type="button" className="act assign" disabled={busy || !assignT.prefixes.length}
          onClick={() => {
            const v = assign.trim()
            const owner = v ? (allUsers().find(u => u.name.toLowerCase() === v.toLowerCase())?.id ?? v) : '@me'
            setPending({ kind: 'assign', label: v ? `assign→${v}` : 'assign→me', owner })
          }}>
          {assign.trim() ? 'assign' : 'assign to me'}
        </button>
        <input list="bb-assign-users" value={assign} onChange={e => setAssign(e.target.value)} placeholder="you" size={7} aria-label="Assign matches to user" />
        <datalist id="bb-assign-users">{allUsers().map(u => <option key={u.id} value={u.name} />)}</datalist>
      </>}
      {canStage && <button type="button" className="act stage" disabled={busy || !stageT.prefixes.length} onClick={() => setPending({ kind: 'stage', label: 'stage' })}>stage for deletion</button>}
      <input value={memo} onChange={e => setMemo(e.target.value)} placeholder="memo" size={10} aria-label="Bulk memo" />
      {progress && <span className="bb-progress">{progress}</span>}
      {done && !progress && <span className="bb-progress">{done}</span>}
      {pending && !progress && (
        <span className="bb-confirm">
          {pending.label} <b>{plural(t.prefixes.length, 'prefix', 'prefixes')}</b> ({fmtBytes(t.b)}, {plural(t.o, 'file')})
          {batches > 1 ? `, sent in ${batches} batches` : ''}?
          {caveats(t, pending.kind).map(c => <span key={c} className="bb-note"> {c}</span>)}
          <button type="button" className="act go" disabled={!t.prefixes.length} onClick={() => void run(pending)}>confirm</button>
          <button type="button" className="act" onClick={() => setPending(null)}>cancel</button>
        </span>
      )}
      {(post.error || stage.error) && <span className="bb-warn">{String((post.error ?? stage.error)?.message)}</span>}
    </span>
  )
}

function ReviewGroup({ parent, items, b, dropped, toggle, fmtBytes }: {
  parent: string
  items: CoverItem[]
  b: number
  dropped: ReadonlySet<string>
  toggle: (paths: string[], drop: boolean) => void
  fmtBytes: (b: number) => string
}) {
  const keptN = items.filter(i => !dropped.has(i.path)).length
  const all = keptN === items.length
  const paths = items.map(i => i.path)
  const shown = items.slice(0, LIST_PER_GROUP)
  return (
    <li className="bb-group">
      <label className="bb-row bb-parent">
        <input type="checkbox" checked={all} ref={el => { if (el) el.indeterminate = keptN > 0 && !all }} onChange={() => toggle(paths, all)} aria-label={`all matches in ${parent || 'the root'}`} />
        <code>{parent ? `${parent}/` : '(root)'}</code>
        <span className="bb-n">{items.length > 1 ? `${keptN.toLocaleString('en-US')}/${items.length.toLocaleString('en-US')} · ` : ''}{fmtBytes(b)}</span>
      </label>
      <ul>
        {shown.map(i => (
          <li key={i.path}>
            <label className="bb-row">
              <input type="checkbox" checked={!dropped.has(i.path)} onChange={() => toggle([i.path], !dropped.has(i.path))} />
              <code>{i.path.slice(parent ? parent.length + 1 : 0)}{i.kind === 'dir' ? '/' : ''}</code>
              <span className="bb-n">{i.kind === 'dir' ? `${plural(i.o, 'file')} · ` : i.kind === null ? 'unchecked · ' : ''}{fmtBytes(i.b)}</span>
            </label>
          </li>
        ))}
        {items.length > shown.length && <li className="bb-more">…and {(items.length - shown.length).toLocaleString('en-US')} more (the folder’s box takes or drops them all)</li>}
      </ul>
    </li>
  )
}
