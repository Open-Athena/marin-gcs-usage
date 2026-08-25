import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { Avatar, whoToHandle } from './Avatar'
import { ACTION_LABELS, useMarks } from './marks'
import { ACTION_COLORS, fmtMarkDate } from './MarkControls'
import { Tooltip } from './Tooltip'
import { type RuleUser, type Rules, groupLabel } from './types'

// Recent-marks activity feed (specs/actions-ledger.md): the ledger's keep +
// owner rows, newest first — who decided what, when. Read-only; the map is
// where marks are set. Clicking a prefix jumps the treemap to it.

interface Event {
  ts: number
  who: string
  prefix: string
  id: number
  label: string
  color: string
  memo: string | null
}

const fmtWhen = (ts: number): string =>
  new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

// `gs://marin-<bucket>/<path>/` → the treemap's `?p=` drill segments.
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

// Avatar + name, hover for the full identity card. Maps the actor's email/id to
// a canonical user via rules.json (by id or alias) for group + aka enrichment.
function WhoCell({ who, user }: { who: string; user?: RuleUser }) {
  const handle = whoToHandle(who)
  const name = user?.u ?? handle
  return (
    <Tooltip content={
      <div className="user-card">
        <div className="uc-head"><Avatar handle={handle} label={name} size={32} /><b>{name}</b></div>
        {user && <div>group: <b>{groupLabel(user.team)}</b></div>}
        <div className="uc-sub">{who}</div>
        {user?.aliases?.length ? <div className="uc-sub">aka {user.aliases.join(', ')}</div> : null}
      </div>
    }>
      <span className="who-chip"><Avatar handle={handle} label={name} size={18} /> {name}</span>
    </Tooltip>
  )
}

export function MarksPage() {
  const { data, isLoading, error } = useMarks(true)
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
  const events = useMemo((): Event[] => {
    if (!data) return []
    const evs: Event[] = []
    for (const r of data.keeps)
      evs.push({
        ts: r.ts, who: r.who, prefix: r.prefix, id: r.action_id, memo: r.memo,
        label: r.keep == null ? 'cleared' : ACTION_LABELS[r.keep],
        color: r.keep == null ? 'var(--line)' : ACTION_COLORS[r.keep],
      })
    for (const r of data.owners)
      evs.push({
        ts: r.ts, who: r.who, prefix: r.prefix, id: r.action_id, memo: r.memo,
        label: r.owner == null ? 'released' : `claimed${r.owner === r.who ? '' : ` for ${r.owner}`}`,
        color: 'var(--t-oa)',
      })
    // Newest first; action_id breaks ties within the same second.
    return evs.sort((a, b) => b.ts - a.ts || b.id - a.id)
  }, [data])

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
      {isLoading && !data && <p className="loading">loading marks…</p>}
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
                  <Link to={`/?p=${encodeURIComponent(prefixToPath(e.prefix))}`}>{e.prefix}</Link>
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
