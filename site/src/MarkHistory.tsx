import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { fmtMarkDate } from './MarkControls'
import { UserChip } from './UserChip'
import { ActionChip, eventsUnder, useMarkEvents } from './markEvents'

// Path-scoped slice of the mark ledger: every keep/sweep/clear/claim under the
// currently-drilled prefix, newest first. The map shows *current* fate; this
// shows how it got there (and, when size-over-time can't, the change story).

const PAGE = 8

const fmtWhen = (ts: number): string =>
  new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

/** `gs://marin-<bucket>/<path>/` → the treemap's URL path segments. */
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

export function MarkHistory({ prefix, scope }: { prefix: string; scope: string }) {
  const { events } = useMarkEvents()
  const [page, setPage] = useState(0)
  const scoped = useMemo(() => eventsUnder(events, prefix), [events, prefix])
  if (scoped.length === 0) return null

  const pages = Math.ceil(scoped.length / PAGE)
  const p = Math.min(page, pages - 1)
  const rows = scoped.slice(p * PAGE, p * PAGE + PAGE)
  const base = prefix.endsWith('/') ? prefix : prefix + '/'

  return (
    <section id="mark-history" className="children-tbl">
      <h2>Mark history</h2>
      <p className="sub">
        Every keep / sweep / clear / claim under <code>{scope}</code>, newest first — {scoped.length} action{scoped.length === 1 ? '' : 's'}.
      </p>
      <table className="worklist marks-feed">
        <thead>
          <tr><th>when</th><th>who</th><th>action</th><th>prefix</th></tr>
        </thead>
        <tbody>
          {rows.map(e => {
            // Show the prefix relative to the current view when it's inside it.
            const rel = e.prefix.startsWith(base) ? e.prefix.slice(base.length) || '(here)' : e.prefix
            return (
              <tr key={`${e.id}-${e.prefix}`}>
                <td title={fmtMarkDate(e.ts)}>{fmtWhen(e.ts)}</td>
                <td><UserChip who={e.who} size={15} /></td>
                <td><ActionChip e={e} /></td>
                <td className="prefix">
                  <Link to={`/${prefixToPath(e.prefix)}`}>{rel}</Link>
                  {e.memo && <span className="memo" title={e.memo}> — {e.memo}</span>}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {pages > 1 && (
        <div className="pager">
          <button type="button" disabled={p === 0} onClick={() => setPage(p - 1)}>← prev</button>
          <span>{p + 1} / {pages}</span>
          <button type="button" disabled={p >= pages - 1} onClick={() => setPage(p + 1)}>next →</button>
        </div>
      )}
    </section>
  )
}
