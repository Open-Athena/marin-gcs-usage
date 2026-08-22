import { useMemo, useState } from 'react'
import { signInUrl, useCanMark } from './auth'
import { ACTION_COLORS, KLC_TIP, MarkControls } from './MarkControls'
import type { MarkAction, MarkIndex } from './marks'
import { ACTION_LABELS, useMarkMutations } from './marks'
import { collectRows, looksCkpt, reviewedBytes, teamLens, userLens } from './sweep'
import type { SweepRow } from './sweep'
import { epochDaysToMonth } from './colors'
import { Tooltip } from './Tooltip'
import type { TreeNode } from './types'
import { useUnits } from './units'

// The /mark review tabs (specs/mark-sweep-ui.md): ranked worklists of
// prefixes per lens — the viewer's own files, unattributed (lost & found),
// and communal — with inline mark buttons, so reviewing is a list-walk
// rather than a treemap hunt. "Everything" = just the treemap below.

export type MarkTab = 'mine' | 'lost' | 'communal' | 'all'

const PAGE = 40

const PREFIX_RE = /^gs:\/\/marin-[a-z0-9-]+\/(?:[^\s]*\/)?$/

export function MarkTabs({ root, idx, myUser, viewUser, setViewUser, users, tab, setTab, scoped, setScoped, openPath }: {
  root: TreeNode
  idx: MarkIndex
  /** The signed-in viewer's own attribution user (labels, "back to me"). */
  myUser: string | null
  /** The user whose files the "mine" tab shows (anyone's view is browsable). */
  viewUser: string | null
  setViewUser: (u: string | undefined) => void
  /** Known attribution user ids, for the picker datalist. */
  users: string[]
  tab: MarkTab
  setTab: (t: MarkTab) => void
  /** Scope the treemap below to this tab's lens (vs the full estate). */
  scoped: boolean
  setScoped: (v: boolean) => void
  /** Drill the treemap to a prefix's path segments (detail view). */
  openPath: (segs: string[]) => void
}) {
  const { fmtBytes } = useUnits()
  const { put, claim, post } = useMarkMutations()
  const canMark = useCanMark()
  const [limit, setLimit] = useState(PAGE)
  const [sel, setSel] = useState<Set<string>>(new Set())
  const [typed, setTyped] = useState('')
  const [userDraft, setUserDraft] = useState<string | null>(null)
  const [bulkPending, setBulkPending] = useState<{ action: MarkAction | null } | null>(null)

  const rows = useMemo<SweepRow[]>(() => {
    if (tab === 'all') return []
    if (tab === 'mine') {
      if (!viewUser) return []
      return collectRows(root, userLens(viewUser), { minBytes: 20e9 }).sort((a, b) => b.b - a.b)
    }
    if (tab === 'lost') {
      // least-recently-created first (atime plane lands later; `d` is the
      // fallback ordering the spec names) — undated rows go last, by size
      return collectRows(root, teamLens('unattributed')).sort(
        (a, b) => (a.node.d ?? Infinity) - (b.node.d ?? Infinity) || b.b - a.b,
      )
    }
    return collectRows(root, teamLens('communal'), { minBytes: 500e9 }).sort((a, b) => b.b - a.b)
  }, [root, tab, viewUser])

  const total = rows.reduce((s, r) => s + r.b, 0)
  const reviewed = reviewedBytes(rows, idx)

  const mineLabel = !viewUser ? 'My files'
    : viewUser === myUser ? `My files (${viewUser})`
    : `${viewUser}'s files`
  const tabs: [MarkTab, string][] = [
    ['mine', mineLabel],
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
  // One batch POST (one ledger transaction) instead of a mutate-per-row loop.
  const bulkWrite = (action: MarkAction | null) => {
    setBulkPending(null)
    post.mutate([...sel].map(uri => ({ pattern: uri + '/', keep: action })))
    setSel(new Set())
  }
  const bulkOv = [...sel].reduce((s, uri) => s + idx.overridesOf(uri).n, 0)
  const bulkMark = (action: MarkAction | null) => (bulkOv > 0 ? setBulkPending({ action }) : bulkWrite(action))

  const typedPrefix = typed.trim().endsWith('/') ? typed.trim() : typed.trim() ? typed.trim() + '/' : ''
  const typedValid = PREFIX_RE.test(typedPrefix)

  // The picker keeps a local draft so it's freely editable (a value-snapping
  // controlled input fights Chrome's datalist and made the field feel stuck):
  // a known user commits immediately (datalist click sends the full value);
  // anything else commits on Enter/blur — exact user, or clear back to "me".
  const draftShown = userDraft ?? viewUser ?? ''
  const commitUser = (v: string) => {
    setUserDraft(null)
    setViewUser(v === '' || v === myUser ? undefined : v)
  }
  const onUserDraft = (v: string) => {
    if (users.includes(v)) commitUser(v)
    else setUserDraft(v)
  }
  const onUserDone = () => {
    if (userDraft === null) return
    const v = userDraft.trim()
    if (v === '' || users.includes(v)) commitUser(v)
    else setUserDraft(null)  // unknown user: revert to the committed value
  }

  return (
    <section className="mark-tabs">
      <div className="tabrow">
        {tabs.map(([t, label]) => (
          <button key={t} type="button" className={tab === t ? 'on' : ''} onClick={() => { setTab(t); setLimit(PAGE); setSel(new Set()) }}>
            {label}
          </button>
        ))}
        {tab === 'mine' && (
          <span className="user-pick">
            <input
              list="mk-users"
              value={draftShown}
              onChange={e => onUserDraft(e.target.value)}
              onBlur={onUserDone}
              onKeyDown={e => { if (e.key === 'Enter') onUserDone() }}
              onFocus={e => e.currentTarget.select()}
              placeholder="view another user's files"
              aria-label="View another user's files"
              size={22}
              spellCheck={false}
            />
            <datalist id="mk-users">
              {users.map(u => <option key={u} value={u} />)}
            </datalist>
            {viewUser && viewUser !== myUser && myUser && (
              <button type="button" onClick={() => { setUserDraft(null); setViewUser(undefined) }}>← back to my files</button>
            )}
          </span>
        )}
        {tab !== 'all' && (
          <label className="scope-toggle" title="On: the treemap below shows only this tab's prefixes (re-aggregated). Off: full estate, this tab's bytes highlighted.">
            <input type="checkbox" checked={scoped} onChange={e => setScoped(e.target.checked)} />
            scope map to this view
          </label>
        )}
      </div>
      {canMark && (
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
      )}
      {typedValid && <MarkControls uri={typedPrefix.slice(0, -1)} idx={idx} />}
      {!canMark && (
        <p className="tab-note guest-note">
          Browsing as a guest — marks and claims are read-only. <a href={signInUrl()}>Sign in</a> with
          your email (Google, or a personal link) to mark.
        </p>
      )}
      {tab === 'all' ? (
        <p className="tab-note">The whole estate — browse and mark in the treemap below.</p>
      ) : tab === 'mine' && !viewUser ? (
        <p className="tab-note">
          Your email isn't mapped to an attribution user yet — ping Ryan (or an admin can add you at{' '}
          <code>/admin/db/user_emails</code>), or pick any user above to view their files. Meanwhile:
          Lost &amp; found, or browse the treemap below.
        </p>
      ) : (
        <>
          <p className="tab-note">
            {tab === 'mine' && <>Prefixes attributed to <b>{viewUser}</b> — <b>{fmtBytes(reviewed)}</b> of <b>{fmtBytes(total)}</b> reviewed ({total ? ((100 * reviewed) / total).toFixed(0) : 0}%). Unmarked = deleted in the sweep.</>}
            {tab === 'lost' && <>Unattributed prefixes, least-recently-created first — claim what's yours, then mark it. Unclaimed + unmarked = deleted.</>}
            {tab === 'communal' && <>Shared corpora / datakit — Rav &amp; Will sign off here. {fmtBytes(reviewed)} of {fmtBytes(total)} reviewed.</>}
          </p>
          {canMark && sel.size > 0 && (
            <div className="bulkbar">
              <b>{sel.size}</b> selected — mark all:
              <button type="button" onClick={() => bulkMark('keep')}>keep</button>
              <Tooltip content={KLC_TIP}>
                <button type="button" onClick={() => bulkMark('keep_last_ckpt')}>last ckpt</button>
              </Tooltip>
              <button type="button" onClick={() => bulkMark('sweep')}>delete</button>
              <button type="button" onClick={() => bulkMark(null)}>clear marks</button>
              <button type="button" onClick={() => setSel(new Set())}>deselect</button>
              {bulkPending && (
                <span className="override-confirm">
                  overrides <b>{bulkOv}</b> more-specific mark{bulkOv === 1 ? '' : 's'} inside the selection —
                  <button type="button" onClick={() => bulkWrite(bulkPending.action)}>
                    {bulkPending.action === null ? 'clear' : ACTION_LABELS[bulkPending.action]} anyway
                  </button>
                  <button type="button" onClick={() => setBulkPending(null)}>cancel</button>
                </span>
              )}
            </div>
          )}
          <table className="worklist">
            <thead>
              <tr>
                {canMark && <th><input type="checkbox" checked={allShownSel} onChange={toggleAll} title="select all shown" /></th>}
                <th>prefix</th><th className="num">{tab === 'mine' ? (viewUser === myUser ? 'your bytes' : 'their bytes') : 'bytes'}</th><th className="num">share</th><th>created</th><th>state</th>
                {canMark && <th>actions</th>}
              </tr>
            </thead>
            <tbody>
              {shown.map(r => (
                <Row key={r.uri} r={r} idx={idx} lost={tab === 'lost'} fmtBytes={fmtBytes} canMark={canMark}
                  selected={sel.has(r.uri)} onToggle={() => toggle(r.uri)}
                  onMark={(action: MarkAction | null) => put.mutate({ prefix: r.uri + '/', action })}
                  onClaim={() => claim.mutate({ prefix: r.uri + '/' })}
                  onOpen={() => openPath(r.uri.slice('gs://'.length).split('/'))}
                />
              ))}
              {!rows.length && <tr><td colSpan={canMark ? 7 : 5}><em>nothing above the size threshold for this lens</em></td></tr>}
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

function Row({ r, idx, lost, fmtBytes, canMark, selected, onToggle, onMark, onClaim, onOpen }: {
  r: SweepRow
  idx: MarkIndex
  lost: boolean
  fmtBytes: (b: number) => string
  canMark: boolean
  selected: boolean
  onToggle: () => void
  onMark: (a: MarkAction | null) => void
  onClaim: () => void
  onOpen: () => void
}) {
  const { mark, own } = idx.resolve(r.uri)
  const cl = idx.claimOf(r.uri)
  const rel = r.uri.slice('gs://'.length)
  return (
    <tr className={selected ? 'sel' : ''}>
      {canMark && <td><input type="checkbox" checked={selected} onChange={onToggle} /></td>}
      <td className="prefix" title={`${r.uri} — click to open in the treemap`}>
        <a role="link" tabIndex={0} onClick={onOpen}>{rel}</a>
      </td>
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
      {canMark && (
        <td className="actions">
          <button type="button" className={own && mark?.action === 'keep' ? 'on' : ''} onClick={() => onMark('keep')}>keep</button>
          {looksCkpt(r.node, r.uri) && (
            <Tooltip content={KLC_TIP}>
              <button type="button" className={own && mark?.action === 'keep_last_ckpt' ? 'on' : ''} onClick={() => onMark('keep_last_ckpt')}>last ckpt</button>
            </Tooltip>
          )}
          <button type="button" className={own && mark?.action === 'sweep' ? 'on' : ''} onClick={() => onMark('sweep')}>delete</button>
          {own && <button type="button" onClick={() => onMark(null)}>clear</button>}
          {lost && !cl && <button type="button" onClick={onClaim}>claim</button>}
        </td>
      )}
    </tr>
  )
}
