import { useState } from 'react'
import { signInUrl, useCanMark } from './auth'
import { MarkControls } from './MarkControls'
import type { MarkIndex } from './marks'
import { Tooltip } from './Tooltip'
import { shortName } from './UserChip'

// The review lenses, folded onto the normal treemap + children view (they used
// to be a separate `/mark` worklist page). Picking a lens filters/scopes the
// current subtree: the map and the children table below both narrow to it, and
// marking stays inline in that table. `all` = no lens (the plain browse view).
export type Lens = 'all' | 'todo' | 'mine' | 'unclaimed' | 'communal'

// Attribution lenses whose "scope to this view" toggle re-aggregates the map to
// just that slice of the current subtree. `todo` is a mark-state lens (it
// narrows the children table instead), and `all` is the unfiltered view.
export const SCOPABLE: Lens[] = ['mine', 'unclaimed', 'communal']

const LENS_LABELS: Record<Lens, string> = {
  all: 'All',
  todo: 'To-do',
  mine: 'My files',
  unclaimed: 'Unclaimed',
  communal: 'Communal',
}

// The key overlap to disambiguate: To-do is the whole untriaged set (owner-
// agnostic, by mark state); Unclaimed is the narrower "no known owner" slice.
const LENS_TIPS: Record<Lens, string> = {
  all: 'The whole estate under the current view — no lens. Browse and mark anywhere in the treemap or the table below.',
  todo: 'Untriaged — every prefix with no keep/sweep mark yet, whoever owns it. The map and table scope to just these; unmarked = deleted at the sweep.',
  mine: 'Prefixes attributed to you (or the user you pick). Scope the current subtree to just your share of it.',
  unclaimed: 'Prefixes with no known owner — nobody has written-and-claimed them. Claim what’s yours to move it under "My files", then decide keep/sweep.',
  communal: 'Shared corpora / datakit — the communal pool, not any one person’s.',
}

const LENS_NOTES: Partial<Record<Lens, string>> = {
  todo: 'Map + children table scoped to prefixes with no keep/sweep decision yet.',
  unclaimed: 'Unattributed prefixes — claim what’s yours, then mark it. Unclaimed + unmarked = deleted.',
  communal: 'Shared corpora / datakit — Rav & Will sign off here.',
}

const PREFIX_RE = /^gs:\/\/marin-[a-z0-9-]+\/(?:[^\s]*\/)?$/

const LENSES: Lens[] = ['all', 'todo', 'mine', 'unclaimed', 'communal']

export function LensBar({ idx, hasEmail, myUser, viewUser, setViewUser, users, lens, setLens, scoped, setScoped }: {
  idx: MarkIndex
  /** Session has an email — gates the "My files" lens (no email = nothing to attribute). */
  hasEmail: boolean
  /** The signed-in viewer's own attribution user (labels, "back to me"). */
  myUser: string | null
  /** The user whose files the "My files" lens shows (anyone's view is browsable). */
  viewUser: string | null
  setViewUser: (u: string | undefined) => void
  /** Known attribution user ids, for the picker datalist. */
  users: string[]
  lens: Lens
  setLens: (l: Lens) => void
  /** Scope the map to the lens (vs highlight over the full estate). */
  scoped: boolean
  setScoped: (v: boolean) => void
}) {
  const canMark = useCanMark()
  const [typed, setTyped] = useState('')
  const [userDraft, setUserDraft] = useState<string | null>(null)

  const mineLabel = !viewUser ? 'My files'
    : viewUser === myUser ? 'My files'
    : `${shortName(viewUser)}’s files`

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
    else setUserDraft(null) // unknown user: revert to the committed value
  }

  return (
    <section className="lens-bar">
      <div className="tabrow">
        <span className="lbl">lens</span>
        {LENSES.filter(l => l !== 'mine' || hasEmail).map(l => (
          <Tooltip key={l} content={LENS_TIPS[l]}>
            <button type="button" className={lens === l ? 'on' : ''} onClick={() => setLens(l)}>
              {l === 'mine' ? mineLabel : LENS_LABELS[l]}
            </button>
          </Tooltip>
        ))}
        {lens === 'mine' && (
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
        {SCOPABLE.includes(lens) && (
          <label className="scope-toggle" title="On: the map re-aggregates to just this lens's prefixes under the current view. Off: full estate, this lens's bytes highlighted.">
            <input type="checkbox" checked={scoped} onChange={e => setScoped(e.target.checked)} />
            scope map to this lens
          </label>
        )}
      </div>
      {lens === 'mine' && !viewUser ? (
        <p className="tab-note">
          Your email isn’t mapped to an attribution user yet — ping Ryan (or an admin can add you at{' '}
          <code>/admin/db/user_emails</code>), or pick any user above to view their files.
        </p>
      ) : LENS_NOTES[lens] && <p className="tab-note">{LENS_NOTES[lens]}</p>}
      {canMark ? (
        <div className="typed-path">
          <input
            value={typed}
            onChange={e => setTyped(e.target.value)}
            placeholder="mark a typed prefix — gs://marin-<bucket>/path/ (any depth, even below the treemap's fold floor)"
            size={56}
            spellCheck={false}
          />
          {typed.trim() !== '' && !typedValid && <span className="err">need gs://marin-&lt;bucket&gt;/path/</span>}
        </div>
      ) : (
        <p className="tab-note guest-note">
          Browsing as a guest — marks and claims are read-only. <a href={signInUrl()}>Sign in</a> with
          your email (Google, or a personal link) to mark.
        </p>
      )}
      {typedValid && <MarkControls uri={typedPrefix.slice(0, -1)} idx={idx} />}
    </section>
  )
}
