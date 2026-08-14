import { useEffect } from 'react'
import type { Rules, TreeNode, UserInfo } from './types'
import { fmtBytes } from './types'

const SLACK_URL = 'https://openathena.slack.com/archives/C0AHF5KV11Q'
const YAML_URL = 'https://github.com/Open-Athena/marin-gcs-usage/blob/main/src/gcs_usage/identities.yaml'

const Note = ({ note }: { note?: string }) =>
  note ? (
    <span className="note">
      {note.startsWith('TODO') && <span className="todo">unconfirmed</span>}
      {note.replace(/^TODO confirm( —|:)? ?/, '')}
    </span>
  ) : null

export function AttributionRules({ rules, tree, users }: {
  rules: Rules
  tree: TreeNode
  users: UserInfo[]
}) {
  // deep-linkable: the section mounts after data loads, so honor #attribution then
  useEffect(() => {
    if (location.hash === '#attribution') document.getElementById('attribution')?.scrollIntoView()
  }, [])

  const bytesOf = new Map(users.map(u => [u.u, u.b]))
  const attributed = users.reduce((s, u) => s + u.b, 0)
  const pct = (b: number) => ((100 * b) / tree.b).toFixed(1)
  const ruleUsers = [...rules.users].sort((a, b) => (bytesOf.get(b.u) ?? 0) - (bytesOf.get(a.u) ?? 0))
  return (
    <section className="attrib" id="attribution">
      <h2>How attribution works</h2>
      <div className="prose">
        <p>
          Ownership is inferred per directory prefix, deepest match wins. Signals, roughly by weight:
          W&B run configs (checkpoint/output paths written by trainers), W&B run-name ↔ directory joins,{' '}
          <code>.executor_info</code> sidecars, provenance records, and the manual rules below.{' '}
          <b>{pct(attributed)}%</b> of bytes are attributed; the rest shows as{' '}
          <i>unattributed</i> (gray). Attribution means “this user's runs wrote these bytes” — a run
          writing to a dir doesn't always mean its owner should pay for it. Treat per-user numbers as a
          starting point, not a bill.
        </p>
        <p className="feedback">
          Spot a wrong owner? Say so in <a href={SLACK_URL} target="_blank" rel="noreferrer">#marin-eng</a> or PR{' '}
          <a href={YAML_URL} target="_blank" rel="noreferrer"><code>identities.yaml</code></a> — aliases and
          prefix rules all live there and take effect on the next data refresh.
        </p>
      </div>
      <details>
        <summary>Users &amp; aliases ({rules.users.length})</summary>
        <table className="classes rulestable">
          <thead>
            <tr><th>user</th><th>attributed</th><th>aliases</th><th>notes</th></tr>
          </thead>
          <tbody>
            {ruleUsers.map(u => (
              <tr key={u.u}>
                <td>{u.u}</td>
                <td>{bytesOf.has(u.u) ? fmtBytes(bytesOf.get(u.u)!) : '—'}</td>
                <td className="aliases">{u.aliases.join(', ')}</td>
                <td><Note note={u.note} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      <details>
        <summary>Manual prefix rules ({rules.prefix_owners.length})</summary>
        <table className="classes rulestable">
          <thead>
            <tr><th>prefix</th><th>owner</th><th>notes</th></tr>
          </thead>
          <tbody>
            {rules.prefix_owners.map(p => (
              <tr key={p.prefix}>
                <td><code>{p.prefix}</code></td>
                <td>{p.user ?? '—'}</td>
                <td><Note note={p.note} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  )
}
