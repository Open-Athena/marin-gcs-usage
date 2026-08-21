import { useMemo, useState } from 'react'
import { ACTION_COLORS, MarkControls } from './MarkControls'
import type { MarkAction, MarkIndex } from './marks'
import { ACTION_LABELS, useMarkMutations } from './marks'
import { collectRows, reviewedBytes, teamLens, userLens } from './sweep'
import type { SweepRow } from './sweep'
import { epochDaysToMonth } from './colors'
import type { TreeNode } from './types'
import { useUnits } from './units'

// The /mark review tabs (specs/mark-sweep-ui.md): ranked worklists of
// prefixes per lens — the viewer's own files, unattributed (lost & found),
// and communal — with inline mark buttons, so reviewing is a list-walk
// rather than a treemap hunt. "Everything" = just the treemap below.

export type MarkTab = 'mine' | 'lost' | 'communal' | 'all'

const PAGE = 40

const PREFIX_RE = /^gs:\/\/marin-[a-z0-9-]+\/(?:[^\s]*\/)?$/

export function MarkTabs({ root, idx, myUser, tab, setTab }: {
  root: TreeNode
  idx: MarkIndex
  myUser: string | null
  tab: MarkTab
  setTab: (t: MarkTab) => void
}) {
  const { fmtBytes } = useUnits()
  const { put, claim } = useMarkMutations()
  const [limit, setLimit] = useState(PAGE)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [typed, setTyped] = useState('')

  const rows = useMemo<SweepRow[]>(() => {
    if (tab === 'all') return []
    if (tab === 'mine') {
      if (!myUser) return []
      return collectRows(root, userLens(myUser), { minBytes: 20e9 }).sort((a, b) => b.b - a.b)
    }
    if (tab === 'lost') {
      // least-recently-created first (atime plane lands later; `d` is the
      // fallback ordering the spec names) — undated rows go last, by size
      return collectRows(root, teamLens('unattributed')).sort(
        (a, b) => (a.node.d ?? Infinity) - (b.node.d ?? Infinity) || b.b - a.b,
      )
    }
    return collectRows(root, teamLens('communal'), { minBytes: 500e9 }).sort((a, b) => b.b - a.b)
  }, [root, tab, myUser])

  const total = rows.reduce((s, r) => s + r.b, 0)
  const reviewed = reviewedBytes(rows, idx)

  const tabs: [MarkTab, string][] = [
    ['mine', myUser ? `My files (${myUser})` : 'My files'],
    ['lost', 'Lost & found'],
    ['communal', 'Communal'],
    ['all', 'Everything'],
  ]

  const shown = rows.slice(0, limit)
  const allShownSel = shown.length > 0 && shown.every(r => sel.has(r.uri))
  const toggleAll = () =>
    setSel(allShownSel ? new Set() : new Set(shown.map(r => r.uri)))
  const toggle = (uri: string) =>
    setSel(s => {
      const n = new Set(s)
      if (n.has(uri)) n.delete(uri)
      else n.add(uri)
      return n
    })
  const bulkMark = (action: MarkAction | null) => {
    for (const uri of sel) put.mutate({ prefix: uri + '/', action })
    setSel(new Set())
  }

  const typedPrefix = typed.trim().endsWith('/') ? typed.trim() : typed.trim() ? typed.trim() + '/' : ''
  const typedValid = PREFIX_RE.test(typedPrefix)

  return (
    <section className="mark-tabs">
      <div className="tabrow">
        {tabs.map(([t, label]) => (
          <button key={t} type="button" className={tab === t ? 'on' : ''} onClick={() => { setTab(t); setLimit(PAGE); setSel(new Set()) }}>
            {label}
          </button>
        ))}
      </div>
      <div className="typed-path">
        <input
          value={typed}
          onChange={e => setTyped(e.target.value)}
          placeholder="mark a typed prefix — gs://marin-<bucket>/path/ (works below the tree's depth cap)"
          size={56}
          spellCheck={false}
        />
        {typed.trim() !== '' && !typedValid && <span className="err">need gs://marin-&lt;bucket&gt;/path/</span>}
      </div>
      {typedValid && <MarkControls uri={typedPrefix.slice(0, -1)} idx={idx} />}
      {tab === 'all' ? (
        <p className="tab-note">The whole estate — browse and mark in the treemap below.</p>
      ) : tab === 'mine' && !myUser ? (
        <p className="tab-note">
          Your email isn't mapped to an attribution user yet — ping Ryan (or an admin can add you at{' '}
          <code>/admin/db/user_emails</code>). Meanwhile: Lost &amp; found, or browse the treemap below.
        </p>
      ) : (
        <>
          <p className="tab-note">
            {tab === 'mine' && <>Prefixes attributed to <b>{myUser}</b> — <b>{fmtBytes(reviewed)}</b> of <b>{fmtBytes(total)}</b> reviewed ({total ? ((100 * reviewed) / total).toFixed(0) : 0}%). Unmarked = deleted in the sweep.</>}
            {tab === 'lost' && <>Unattributed prefixes, least-recently-created first — claim what's yours, then mark it. Unclaimed + unmarked = deleted.</>}
            {tab === 'communal' && <>Shared corpora / datakit — Rav &amp; Will sign off here. {fmtBytes(reviewed)} of {fmtBytes(total)} reviewed.</>}
          </p>
          {sel.size > 0 && (
            <div className="bulkbar">
              <b>{sel.size}</b> selected — mark all:
              <button type="button" onClick={() => bulkMark('keep')}>keep</button>
              <button type="button" onClick={() => bulkMark('keep_last_ckpt')}>last ckpt</button>
              <button type="button" onClick={() => bulkMark('delete')}>delete</button>
              <button type="button" onClick={() => bulkMark(null)}>clear marks</button>
              <button type="button" onClick={() => setSel(new Set())}>deselect</button>
            </div>
          )}
          <table className="worklist">
            <thead>
              <tr>
                <th><input type="checkbox" checked={allShownSel} onChange={toggleAll} title="select all shown" /></th>
                <th>prefix</th><th className="num">{tab === 'mine' ? 'your bytes' : 'bytes'}</th><th className="num">share</th><th>created</th><th>state</th><th>actions</th>
              </tr>
            </thead>
            <tbody>
              {shown.map(r => (
                <Row key={r.uri} r={r} idx={idx} lost={tab === 'lost'} fmtBytes={fmtBytes}
                  selected={sel.has(r.uri)} onToggle={() => toggle(r.uri)}
                  onMark={(action: MarkAction | null) => put.mutate({ prefix: r.uri + '/', action })}
                  onClaim={() => claim.mutate({ prefix: r.uri + '/' })}
                />
              ))}
              {!rows.length && <tr><td colSpan={7}><em>nothing above the size threshold for this lens</em></td></tr>}
            </tbody>
          </table>
          {rows.length > limit && (
            <button type="button" className="more" onClick={() => setLimit(l => l + PAGE)}>
              show {Math.min(PAGE, rows.length - limit)} more of {rows.length - limit}
            </button>
          )}
          {(put.error ?? claim.error) && <p className="err">{(put.error ?? claim.error)!.message}</p>}
        </>
      )}
    </section>
  )
}

function Row({ r, idx, lost, fmtBytes, selected, onToggle, onMark, onClaim }: {
  r: SweepRow
  idx: MarkIndex
  lost: boolean
  fmtBytes: (b: number) => string
  selected: boolean
  onToggle: () => void
  onMark: (a: MarkAction | null) => void
  onClaim: () => void
}) {
  const { mark, own } = idx.resolve(r.uri)
  const cl = idx.claimOf(r.uri)
  const rel = r.uri.slice('gs://'.length)
  return (
    <tr className={selected ? 'sel' : ''}>
      <td><input type="checkbox" checked={selected} onChange={onToggle} /></td>
      <td className="prefix" title={r.uri}>{rel}</td>
      <td className="num">{fmtBytes(r.b)}</td>
      <td className="num">{(100 * r.frac).toFixed(0)}%</td>
      <td>{r.node.d != null ? epochDaysToMonth(r.node.d) : '—'}</td>
      <td>
        {mark ? (
          <span className="chip" title={`${mark.who}${own ? '' : ` (inherited from ${mark.prefix})`}`}
            style={{ borderColor: ACTION_COLORS[mark.action] }}>
            <span className="sw" style={{ background: ACTION_COLORS[mark.action] }} />
            {ACTION_LABELS[mark.action]}{own ? '' : ' ⌃'}
          </span>
        ) : (
          <span className="chip unmarked"><span className="sw" style={{ background: 'var(--mk-del)' }} />unmarked</span>
        )}
        {cl && <span className="chip claim" title={`claimed by ${cl.who}`}>@{cl.who.split('@')[0]}</span>}
      </td>
      <td className="actions">
        <button type="button" className={own && mark?.action === 'keep' ? 'on' : ''} onClick={() => onMark('keep')}>keep</button>
        <button type="button" className={own && mark?.action === 'keep_last_ckpt' ? 'on' : ''} onClick={() => onMark('keep_last_ckpt')} title="keep only the highest-step checkpoint in each run">last ckpt</button>
        <button type="button" className={own && mark?.action === 'delete' ? 'on' : ''} onClick={() => onMark('delete')}>delete</button>
        {own && <button type="button" onClick={() => onMark(null)}>clear</button>}
        {lost && !cl && <button type="button" onClick={onClaim}>claim</button>}
      </td>
    </tr>
  )
}
