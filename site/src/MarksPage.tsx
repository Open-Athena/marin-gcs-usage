import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { whoToHandle } from './Avatar'
import { UserChip } from './UserChip'
import { fmtMarkDate } from './MarkControls'
import { useMarkEvents } from './markEvents'
import { type RuleUser, type Rules } from './types'

// Recent-marks activity feed (specs/actions-ledger.md): the ledger's keep +
// owner rows, newest first — who decided what, when. Read-only; the map is
// where marks are set. Clicking a prefix jumps the treemap to it.

const fmtWhen = (ts: number): string =>
  new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

// `gs://marin-<bucket>/<path>/` → the treemap's URL path (below the store root).
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

// Avatar + short name, with the interactive identity hover card (UserChip). The
// rules.json row adds the "aka <aliases>" line the registry alone doesn't carry.
function WhoCell({ who, user }: { who: string; user?: RuleUser }) {
  const extra = user?.aliases?.length
    ? <div className="uc-sub">aka {user.aliases.join(', ')}</div>
    : undefined
  return <UserChip who={who} extra={extra} />
}

export function MarksPage() {
  const { events, isLoading, error } = useMarkEvents()
  const { data: rules } = useQuery<Rules>({
    queryKey: ['rules'],
    queryFn: async () => {
      const r = await fetch('/data/rules.json')
      if (!r.ok) throw new Error(`rules: ${r.status}`)
      return r.json()
    },
    retry: false,
  })
  const userByHandle = useMemo(() => {
    const m = new Map<string, RuleUser>()
    for (const u of rules?.users ?? []) {
      m.set(u.u, u)
      for (const a of u.aliases) m.set(a.toLowerCase(), u)
    }
    return m
  }, [rules])

  return (
    <main className="marks-page">
      <header>
        <div className="hrow">
          <h1>Recent marks</h1>
          <Link className="nav-files" to="/" style={{ fontSize: '0.9em' }}>←&nbsp;Back&nbsp;to&nbsp;the&nbsp;map</Link>
        </div>
        <p className="sub">
          Every keep / sweep / claim in the ledger, newest first. Click a prefix to open it on the map.
          {events.length > 0 && <> {events.length} action{events.length === 1 ? '' : 's'}.</>}
        </p>
      </header>

      {error && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load marks: {error.message}</p>}
      {isLoading && events.length === 0 && <p className="loading">loading marks…</p>}
      {!isLoading && !error && events.length === 0 && <p className="tab-note">No marks yet — head to the map and start marking.</p>}

      {events.length > 0 && (
        <table className="worklist marks-feed">
          <thead>
            <tr><th>when</th><th>who</th><th>action</th><th>prefix</th></tr>
          </thead>
          <tbody>
            {events.map(e => (
              <tr key={`${e.id}-${e.prefix}`}>
                <td title={fmtMarkDate(e.ts)}>{fmtWhen(e.ts)}</td>
                <td><WhoCell who={e.who} user={userByHandle.get(whoToHandle(e.who))} /></td>
                <td>
                  <span className="chip" style={{ borderColor: e.color }}>
                    <span className="sw" style={{ background: e.color }} />
                    {e.label}
                  </span>
                </td>
                <td className="prefix">
                  <Link to={`/${prefixToPath(e.prefix)}`}>{e.prefix}</Link>
                  {e.memo && <span className="memo" title={e.memo}> — {e.memo}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}
