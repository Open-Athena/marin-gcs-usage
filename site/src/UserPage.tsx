import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Avatar } from './Avatar'
import { ACTION_COLORS, fmtMarkDate } from './MarkControls'
import { ACTION_LABELS, useMarkIndex, useMarks, type Mark, type MarkIndex } from './marks'
import { DEFAULT_STORE } from './stores'
import { allUserFates, type Fate } from './sweep'
import { UserChip, canonId, ghHandle, shortName, teamOf } from './UserChip'
import {
  GROUP_LABELS, ratePerByte, fmtBytesIec, fmtN, fmtUsd,
  type Meta, type TreeNode,
} from './types'

// Per-user estate pages (the view Ahmed went looking for and couldn't find):
// `/users` ranks everyone by attributed bytes; `/user/:id` answers "of my
// N TiB, what's keep-marked, what's sweep-marked, and what's still undecided?"
// — the fate rollup the map's per-prefix chips never total up.
//
// Fate is computed the same way the treemap overlay resolves it (most recent
// live mark on an ancestor-or-equal prefix wins): walk the scan tree, carry
// the user's attributed share (`us` is additive and untruncated, so a zero
// share prunes the subtree), and stop at any node whose subtree holds no
// deeper marks — every byte there shares one resolved fate. Residual bytes at
// mixed nodes (folded `(other)` tiles, share not covered by kept children)
// take the node's own resolved fate.

interface FateRow {
  uri: string          // marked prefix (decided) or maximal clean subtree (unmarked)
  fate: Fate
  b: number            // this user's bytes governed by the row
  mark: Mark | null
}

const share = (n: TreeNode, uid: string): number => n.us?.find(([u]) => u === uid)?.[1] ?? 0

function userFates(root: TreeNode, uid: string, idx: MarkIndex): FateRow[] {
  const decided = new Map<string, { mark: Mark; b: number }>()
  const undecided = new Map<string, number>()
  const settle = (uri: string, ub: number, mark: Mark | null) => {
    if (mark) {
      const cur = decided.get(mark.prefix)
      if (cur) cur.b += ub
      else decided.set(mark.prefix, { mark, b: ub })
    } else undecided.set(uri, (undecided.get(uri) ?? 0) + ub)
  }
  const walk = (n: TreeNode, uri: string, ub: number) => {
    if (ub <= 0) return
    const { mark, under } = idx.resolve(uri)
    if (under === 0) return settle(uri, ub, mark)
    let rest = ub
    for (const c of n.c ?? []) {
      if (c.n.startsWith('(')) continue
      const cb = share(c, uid)
      if (cb <= 0) continue
      rest -= cb
      walk(c, `${uri}/${c.n}`, cb)
    }
    // Folded tiles + share past the tree's floor stay here, under this
    // node's own resolved fate — deeper marks may exist inside, but the
    // tree can't see past its own truncation.
    if (rest > 0) settle(uri, rest, mark)
  }
  for (const bucket of root.c ?? []) walk(bucket, `gs://${bucket.n}`, share(bucket, uid))
  const rows: FateRow[] = []
  for (const [, { mark, b }] of decided) rows.push({ uri: mark.prefix, fate: mark.action, b, mark })
  for (const [uri, b] of undecided) rows.push({ uri, fate: 'unmarked', b, mark: null })
  return rows.sort((a, b) => b.b - a.b)
}

const FATES: Fate[] = ['keep', 'keep_last_ckpt', 'sweep', 'unmarked']
const fateLabel = (f: Fate): string => (f === 'unmarked' ? 'unmarked' : ACTION_LABELS[f])
const fateColor = (f: Fate): string => (f === 'unmarked' ? 'var(--t-unattr)' : ACTION_COLORS[f])

// `gs://marin-<bucket>/<path>/` → the treemap's URL path.
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

const store = DEFAULT_STORE

function useLatestScan() {
  const scansQ = useQuery<string[]>({
    queryKey: ['scans', store.key],
    queryFn: async () => {
      const r = await fetch(`${store.base}/scans.json`)
      if (!r.ok) throw Object.assign(new Error(`scans: ${r.status}`), { status: r.status })
      return r.json()
    },
  })
  return scansQ.data?.[0] ?? null
}

function useScanFile<T>(name: string, asof: string | null) {
  return useQuery<T>({
    queryKey: [name, store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
}

export function UsersPage() {
  const asof = useLatestScan()
  const metaQ = useScanFile<Meta>('meta', asof)
  const treeQ = useScanFile<TreeNode>('tree', asof)
  const marksQ = useMarks(true)
  const idx = useMarkIndex(marksQ.data)
  const users = useMemo(
    () => [...(metaQ.data?.users ?? [])].sort((a, b) => b.b - a.b),
    [metaQ.data],
  )
  const mixes = metaQ.data?.user_class_bytes
  const fates = useMemo(
    () => (treeQ.data ? allUserFates(treeQ.data, idx) : null),
    [treeQ.data, idx],
  )
  const klc = users.some(u => (fates?.get(u.u)?.keep_last_ckpt ?? 0) > 0)
  const cell = (u: string, f: Fate) => {
    const b = fates?.get(u)?.[f] ?? 0
    return (
      <td className="num" style={b ? { color: fateColor(f) } : { color: 'var(--ink-2)', opacity: 0.5 }}>
        {fates ? (b ? fmtBytesIec(b, true) : '—') : '…'}
      </td>
    )
  }
  return (
    <main className="marks-page user-page">
      <header>
        <div className="hrow">
          <h1>Users</h1>
          <Link className="nav-files" to="/" style={{ fontSize: '0.9em' }}>←&nbsp;Back&nbsp;to&nbsp;the&nbsp;map</Link>
        </div>
        <p className="sub">Everyone with attributed storage{asof && <> in the {asof} scan</>}, largest first — and where their bytes stand (keep / sweep / no decision yet). Click through for the per-prefix breakdown.</p>
      </header>
      {metaQ.isLoading && <p className="loading">loading…</p>}
      {users.length > 0 && (
        <table className="worklist">
          <thead>
            <tr>
              <th>user</th><th>group</th><th className="num">attributed</th><th className="num">est. $/mo</th>
              <th className="num">keep</th>{klc && <th className="num">last-ckpt</th>}<th className="num">sweep</th><th className="num">unmarked</th>
            </tr>
          </thead>
          <tbody>
            {users.map(u => (
              <tr key={u.u}>
                <td><Link className="user-link" to={`/user/${u.u}`}><UserChip who={u.u} /></Link></td>
                <td>{GROUP_LABELS[u.t] ?? u.t}</td>
                <td className="num">{fmtBytesIec(u.b, true)}</td>
                <td className="num">{mixes?.[u.u] ? fmtUsd(ratePerByte(mixes[u.u]) * u.b) : '—'}</td>
                {cell(u.u, 'keep')}
                {klc && cell(u.u, 'keep_last_ckpt')}
                {cell(u.u, 'sweep')}
                {cell(u.u, 'unmarked')}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  )
}

const PAGE = 25

function FateTable({ rows, empty }: { rows: FateRow[]; empty: string }) {
  const [page, setPage] = useState(0)
  if (!rows.length) return <p className="tab-note">{empty}</p>
  const pages = Math.ceil(rows.length / PAGE)
  const p = Math.min(page, pages - 1)
  const slice = rows.slice(p * PAGE, p * PAGE + PAGE)
  return (
    <>
      <table className="worklist marks-feed">
        <thead>
          <tr><th>fate</th><th className="num">your data</th><th>prefix</th><th>by</th><th>when</th></tr>
        </thead>
        <tbody>
          {slice.map(r => (
            <tr key={r.uri}>
              <td>
                <span className="chip" style={{ borderColor: fateColor(r.fate), color: fateColor(r.fate) }}>
                  {fateLabel(r.fate)}
                </span>
              </td>
              <td className="num">{fmtBytesIec(r.b, true)}</td>
              <td className="prefix">
                <Link to={`/${prefixToPath(r.uri)}`}>{r.uri}</Link>
                {r.mark?.note && <span className="memo" title={r.mark.note}> — {r.mark.note}</span>}
              </td>
              <td>{r.mark ? <UserChip who={r.mark.who} /> : ''}</td>
              <td>{r.mark ? fmtMarkDate(r.mark.ts) : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {pages > 1 && (
        <div className="pager">
          <button type="button" disabled={p === 0} onClick={() => setPage(p - 1)}>← prev</button>
          <span className="pager-count">{p * PAGE + 1}–{p * PAGE + slice.length} of {rows.length}</span>
          <button type="button" disabled={p >= pages - 1} onClick={() => setPage(p + 1)}>next →</button>
        </div>
      )}
    </>
  )
}

export function UserPage() {
  const { id = '' } = useParams()
  const asof = useLatestScan()
  const treeQ = useScanFile<TreeNode>('tree', asof)
  const metaQ = useScanFile<Meta>('meta', asof)
  const marksQ = useMarks(true)
  const idx = useMarkIndex(marksQ.data)

  const rows = useMemo(
    () => (treeQ.data ? userFates(treeQ.data, id, idx) : []),
    [treeQ.data, id, idx],
  )
  const totals = useMemo(() => {
    const t = new Map<Fate, { b: number; n: number }>(FATES.map(f => [f, { b: 0, n: 0 }]))
    for (const r of rows) {
      const cur = t.get(r.fate)!
      cur.b += r.b
      cur.n++
    }
    return t
  }, [rows])
  const attributed = useMemo(() => rows.reduce((s, r) => s + r.b, 0), [rows])
  const metaB = metaQ.data?.users?.find(u => u.u === id)?.b
  const mix = metaQ.data?.user_class_bytes?.[id]
  const authored = useMemo(
    () => new Set((marksQ.data?.keeps ?? []).filter(r => r.keep != null && canonId(r.who) === id).map(r => r.prefix)).size,
    [marksQ.data, id],
  )
  const decidedRows = rows.filter(r => r.fate !== 'unmarked')
  const undecidedRows = rows.filter(r => r.fate === 'unmarked')
  const team = teamOf(id)
  const loading = !asof || treeQ.isLoading || marksQ.isLoading

  return (
    <main className="marks-page user-page">
      <header>
        <div className="hrow">
          <h1 className="user-head">
            <Avatar github={ghHandle(id)} name={shortName(id)} size={36} />
            {shortName(id)}
            {team && <span className="uc-group" data-team={team}>{GROUP_LABELS[team] ?? team}</span>}
          </h1>
          <span style={{ display: 'inline-flex', gap: '1.2em' }}>
            <Link className="nav-files" to="/users" style={{ fontSize: '0.9em' }}>All&nbsp;users</Link>
            <Link className="nav-files" to="/" style={{ fontSize: '0.9em' }}>←&nbsp;Back&nbsp;to&nbsp;the&nbsp;map</Link>
          </span>
        </div>
        <p className="sub">
          {fmtBytesIec(attributed, true)} attributed in the {asof ?? '…'} scan
          {mix != null && metaB != null && <> · est. {fmtUsd(ratePerByte(mix) * metaB)}/mo</>}
          {authored > 0 && <> · {fmtN(authored)} prefixes marked by {shortName(id)}</>}.
          Fate of every byte, resolved the way the map does it (most recent mark on an ancestor-or-equal prefix wins).
        </p>
      </header>

      {loading && <p className="loading">loading…</p>}
      {marksQ.error && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load marks: {marksQ.error.message}</p>}
      {!loading && !rows.length && <p className="tab-note">No attributed data for “{id}” in this scan.</p>}

      {rows.length > 0 && (
        <>
          <div className="fate-strip">
            {FATES.map(f => {
              const { b, n } = totals.get(f)!
              if (!b) return null
              return (
                <div className="fate-cell" key={f} style={{ borderColor: fateColor(f) }}>
                  <b style={{ color: fateColor(f) }}>{fateLabel(f)}</b>
                  <span className="fate-b">{fmtBytesIec(b, true)}</span>
                  <span className="fate-n">{fmtN(n)} prefix{n === 1 ? '' : 'es'} · {attributed ? Math.round((b / attributed) * 100) : 0}%</span>
                </div>
              )
            })}
          </div>

          <h2>Decided</h2>
          <p className="tab-note">Prefixes whose mark governs your bytes — yours and anyone else’s marks both count.</p>
          <FateTable rows={decidedRows} empty="Nothing marked yet." />

          <h2>Undecided</h2>
          <p className="tab-note">Your largest subtrees with no keep / sweep decision anywhere above or below — swept by default once the review window closes (date TBD).</p>
          <FateTable rows={undecidedRows} empty="Every attributed byte has a decision. 🎉" />
        </>
      )}
    </main>
  )
}
