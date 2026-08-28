import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useCanMark, useIdent, useSignOut } from './auth'
import { IDENTITIES } from './identities.gen'
import { useMyUser, useUserEmails } from './sweep'
import TokenModal from './TokenModal'
import { UserChip } from './UserChip'

// Shared page chrome: cross-page nav links + the signed-in identity chip. The
// home page embeds it `inline` in its own header row (its h1 + store switcher
// lead); every other page renders the full bar (brand link + links + chip), so
// "where am I / who am I" reads the same everywhere.
export function SiteNav({ inline = false }: { inline?: boolean }) {
  const { pathname } = useLocation()
  const ident = useIdent()
  const canMark = useCanMark()
  const signOut = useSignOut()
  const markLinks = canMark
  const myUser = useMyUser(ident?.email, canMark)
  const emails = useUserEmails(canMark)
  const [tokenOpen, setTokenOpen] = useState(false)
  const link = (to: string, label: string) => {
    const here = to === '/' ? pathname === '/' : pathname === to || pathname.startsWith(to + '/')
    return <Link className="nav-files" to={to} aria-current={here ? 'page' : undefined}>{label}</Link>
  }
  const body = (
    <>
      {tokenOpen && <TokenModal onClose={() => setTokenOpen(false)} />}
      {!inline && <Link className="brand" to="/">Marin GCS usage</Link>}
      <span className="nav-links">
        {link('/files', 'Browse scans →')}
        {markLinks && link('/users', 'Users →')}
        {markLinks && link('/marks', 'Recent marks →')}
      </span>
      {ident && (
        <div className="whoami">
          <UserChip who={myUser ?? ident.email} size={22} extra={<SessionLines email={ident.email} user={myUser} emails={emails} />} />
          {canMark && (
            <button className="token-btn" type="button" onClick={() => setTokenOpen(true)} title="Personal token for agents / CLI">
              token
            </button>
          )}
          <button className="logout" type="button" onClick={signOut}>log out</button>
        </div>
      )}
    </>
  )
  return inline ? body : <div className="hrow site-nav">{body}</div>
}

// Session-specific lines appended to the header chip's identity card (the
// card itself — avatar, name, group, GitHub, storage link — is the shared
// <UserCard>): which sign-in email this session is, the user's other aliases
// and sign-in emails, or a warning when the email maps to no user.
function SessionLines({ email, user, emails }: { email: string; user: string | null; emails?: Record<string, string> }) {
  const aliases = user ? Object.keys(IDENTITIES).filter(k => k !== user && IDENTITIES[k].u === user) : []
  const others = user && emails ? Object.keys(emails).filter(e => emails[e] === user && e !== email.toLowerCase()) : []
  return (
    <div className="uc-session">
      <div>signed in as <code>{email}</code></div>
      {user ? (
        <>
          {aliases.length > 0 && <div>aliases: {aliases.map(a => <code key={a}>{a}</code>)}</div>}
          {others.length > 0 && <div>also signs in as: {others.map(e => <code key={e}>{e}</code>)}</div>}
        </>
      ) : (
        <div className="uc-warn">not mapped to an attribution user — "My files" won't resolve; ping Ryan.</div>
      )}
    </div>
  )
}
