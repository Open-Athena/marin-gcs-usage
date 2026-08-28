import { Treemap, type CellStyle } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { stringParam, useUrlState } from 'use-prms'
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Avatar } from './Avatar'
import { ACTION_COLORS, fmtMarkDate } from './MarkControls'
import { ACTION_LABELS, useMarkIndex, useMarks, type Mark, type MarkIndex } from './marks'
import { DEFAULT_STORE } from './stores'
import { applyFilter, applyNodeFilter } from './filterTree'
import { allUserFates, klcFateAt, klcKeptWithin, klcSplits, lensNodePred, userLens, type Fate, type KlcIndex } from './sweep'
import { Treemap as MarkTreemap } from './Treemap'
import { Tooltip } from './Tooltip'
import { UserChip, canonId, ghHandle, shortName, shortUserKey, teamOf } from './UserChip'
import {
  CLASS_NAMES, CLASS_PRICE_US, GROUP_LABELS, TEAM_VARS,
  ratePerByte, fmtBytesIec, fmtN, fmtUsd,
  type Meta, type TreeNode, type UserInfo,
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

// `keep_last_ckpt` decomposes into real keep/sweep proportions wherever
// possible (`klcSplits` — last-ckpt children kept, siblings swept); the
// walkers do that themselves when handed a KlcIndex, so only bytes under
// *unresolvable* KLC marks reach this fold, where they count as keep.
// Individual mark rows still show the first-class amber "keep last ckpt".
export type ShownFate = 'keep' | 'sweep' | 'unmarked'
const SHOWN_FATES: ShownFate[] = ['keep', 'sweep', 'unmarked']
const ALL_FATES: Fate[] = ['keep', 'keep_last_ckpt', 'sweep', 'unmarked']
const FATE_ORDER_TOTAL = (f: Record<Fate, number>): number => ALL_FATES.reduce((s, k) => s + f[k], 0)
const foldFates = (f: Record<Fate, number>): Record<ShownFate, number> => ({
  keep: f.keep + f.keep_last_ckpt,
  sweep: f.sweep,
  unmarked: f.unmarked,
})
const fateLabel = (f: Fate): string => (f === 'unmarked' ? 'unmarked' : ACTION_LABELS[f])
// `unmarked` gets the regular secondary ink, not the unattributed-gray — as
// the most common column value it has to be readable, not washed out.
const fateColor = (f: Fate): string => (f === 'unmarked' ? 'var(--ink-2)' : ACTION_COLORS[f])

// `gs://marin-<bucket>/<path>/` → the treemap's URL path.
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

const store = DEFAULT_STORE

// The live Google Sheet mirror of this table (created 2026-08-27; re-seed
// with: `gcs-usage report -a <actions.json> -o mark-status.csv` then
// `gws drive files update --params '{"fileId":"<id>","uploadType":"multipart"}'
//   --upload mark-status.csv --upload-content-type text/csv`).
const SHEET_URL = 'https://docs.google.com/spreadsheets/d/1k_11LA21g8uqMckPhkKvwrnRENVKF8yHxbW5NnUiRFc/edit'

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

// Group badge: real logo glyphs (block-S / OA branch mark, cropped from the
// www site's brand SVGs into `public/groups/`) with the full name on hover;
// unknown renders as a plain dash.
const GROUP_ICONS: Record<string, string> = { stanford: '/groups/su.png', oa: '/groups/oa.svg' }

function GroupBadge({ team, tip = true }: { team: string; tip?: boolean }) {
  const icon = GROUP_ICONS[team]
  const el = icon
    ? <img className="grp-icon" src={icon} alt={GROUP_LABELS[team] ?? team} />
    : <span className="grp-none">–</span>
  // tip=false when the badge sits inside a UserChip's hover target — a nested
  // tooltip there would fight the user card.
  return tip ? <Tooltip content={GROUP_LABELS[team] ?? team}>{el}</Tooltip> : el
}

// Est. $/mo with the storage-class mix behind it on hover.
function DollarCell({ b, mix }: { b: number; mix?: Record<string, number> }) {
  if (!mix) return <>—</>
  const rows = Object.entries(mix)
    .sort(([a], [c]) => Number(a) - Number(c))
    .map(([cls, cb]) => ({
      name: CLASS_NAMES[cls] ?? cls,
      b: cb,
      usd: (cb / 1024 ** 3) * (CLASS_PRICE_US[cls] ?? 0.02),
    }))
  return (
    <Tooltip content={
      <table className="class-tt">
        <tbody>
          {rows.map(r => (
            <tr key={r.name}>
              <td>{r.name}</td>
              <td className="num">{fmtBytesIec(r.b)}</td>
              <td className="num">{fmtUsd(r.usd)}/mo</td>
            </tr>
          ))}
        </tbody>
      </table>
    }>
      <span className="has-tt">{fmtUsd(ratePerByte(mix) * b)}</span>
    </Tooltip>
  )
}

// One-level treemap of the whole estate by owner: every user, plus the
// ownerless pools (communal, shared-in-group, unattributed).
interface OwnerCell {
  n: string
  b: number
  team?: string
  id?: string      // canonical user id → /user/:id
  c?: OwnerCell[]
}

// Tile background: the group color pulled well toward dark, so the full-
// strength keep/sweep stripes read as *marks on* the tile, not more of it.
const tileBg = (team: string): string =>
  `color-mix(in oklab, var(${TEAM_VARS[team] ?? '--t-unknown'}) 48%, #131311)`
const USER_TILE_BG = 'color-mix(in oklab, var(--ink) 7%, var(--panel))'

/** The owner tiles (users + ownerless pools) — ONE derivation shared by the
 * map and its legend, so the legend can never key a group absent from the
 * tiles. Live fates (claims applied) win over scan meta when loaded. */
function ownerCells(meta: Meta, fates: Map<string, Record<Fate, number>> | null): OwnerCell[] {
  const metaUsers: UserInfo[] = meta.users ?? []
  const users: { u: string; t: string; b: number }[] = fates
    ? [...fates.entries()]
        .map(([u, f]) => ({
          u,
          t: teamOf(u) ?? metaUsers.find(m => m.u === u)?.t ?? 'unknown',
          b: FATE_ORDER_TOTAL(f),
        }))
        .filter(x => x.b > 0)
    : metaUsers
  const cells: OwnerCell[] = users.map(u => ({ n: shortName(u.u), id: u.u, b: u.b, team: u.t }))
  const teamTotal = (t: string) =>
    Object.values(meta.team_class_bytes?.[t] ?? {}).reduce((s, b) => s + b, 0)
  const userSum = users.reduce((s, u) => s + u.b, 0)
  const communal = teamTotal('communal')
  const unattr = Math.max(0, meta.total_bytes - userSum - communal)
  if (communal > 0) cells.push({ n: 'communal', b: communal, team: 'communal' })
  if (unattr > 0) cells.push({ n: 'unclaimed', b: unattr, team: 'unattributed' })
  return cells.sort((a, b) => b.b - a.b)
}

function MapLegend({ cells, fates }: {
  cells: OwnerCell[]
  fates: Map<string, Record<Fate, number>> | null
}) {
  // Only the POOL tiles carry group colors now — user tiles are fate-striped.
  const groups = [...new Set(cells.filter(c => !c.id).map(c => c.team).filter((t): t is string => !!t))]
  const present: Record<ShownFate, boolean> = { keep: false, sweep: false, unmarked: false }
  if (fates) {
    for (const f of fates.values()) {
      const s = foldFates(f)
      for (const k of SHOWN_FATES) if (s[k] > 0) present[k] = true
    }
  }
  return (
    <div className="map-legend">
      {groups.map(t => (
        <span key={t}><i style={{ background: tileBg(t) }} />{t === 'unattributed' ? 'unclaimed' : GROUP_LABELS[t] ?? t}</span>
      ))}
      {(!fates || present.keep || present.sweep) && <span className="sep" />}
      {(!fates || present.keep) && <span><i style={{ background: 'var(--mk-keep)' }} />keep</span>}
      {(!fates || present.sweep) && <span><i style={{ background: 'var(--mk-del)' }} />sweep</span>}
      {(!fates || present.unmarked) && <span><i style={{ background: 'var(--other)' }} />undecided</span>}
    </div>
  )
}

function UsersMap({ meta, fates, redact = false }: {
  meta: Meta
  fates: Map<string, Record<Fate, number>> | null
  /** og:image mode — names + stripes only: no sizes, no tooltips, no drill. */
  redact?: boolean
}) {
  const navigate = useNavigate()
  const root = useMemo(
    (): OwnerCell => ({ n: '', b: meta.total_bytes, c: ownerCells(meta, fates) }),
    [meta, fates],
  )
  return (
    <div className="users-map">
      <Treemap<OwnerCell>
        root={root}
        getSize={n => n.b}
        getChildren={n => n.c}
        getLabel={n => n.n}
        formatSize={redact ? () => '' : n => fmtBytesIec(n)}
        chrome={false}
        fullscreen={false}
        colorForCell={(n): CellStyle | null => {
          // ONE categorical axis per tile: user tiles carry their fate makeup
          // (neutral base + full-strength keep/sweep/undecided stripes —
          // group would be a second axis fighting the same channel); the
          // ownerless pools, which have no fate stripes, keep their own
          // colors.
          if (!n.id) return n.team ? { bg: tileBg(n.team) } : null
          const style: CellStyle = { bg: USER_TILE_BG }
          const raw = fates?.get(n.id)
          const f = raw ? foldFates(raw) : undefined
          if (f) {
            const total = SHOWN_FATES.reduce((s, k) => s + f[k], 0)
            if (total > 0) {
              const segs = SHOWN_FATES.filter(k => f[k] > 0)
                .map(k => ({
                  color: k === 'unmarked' ? 'var(--other)' : fateColor(k),
                  frac: f[k] / total,
                }))
              if (segs.length > 1) style.segments = segs
              else if (segs.length === 1) style.bg = segs[0].color
            }
          }
          return style
        }}
        renderTooltip={(n) => {
          if (redact) return null
          const raw = n.id ? fates?.get(n.id) : undefined
          const f = raw ? foldFates(raw) : undefined
          const total = f ? SHOWN_FATES.reduce((s, k) => s + f[k], 0) : 0
          return (
            <div>
              <b>{n.n}</b>{n.team && <> · {GROUP_LABELS[n.team] ?? n.team}</>}
              <div>{fmtBytesIec(n.b, true)} · {meta.total_bytes ? ((100 * n.b) / meta.total_bytes).toFixed(1) : 0}%</div>
              {f && total > 0 && (
                <div>
                  {SHOWN_FATES.filter(k => f[k] > 0).map(k => (
                    <span key={k} style={{ color: fateColor(k), marginRight: 8 }}>{fateLabel(k)} {fmtBytesIec(f[k])}</span>
                  ))}
                </div>
              )}
              {n.id && <div className="tt-hint">click for breakdown</div>}
            </div>
          )
        }}
        // Real anchors (this map is one flat level, so every tile qualifies):
        // Vimium hints, cmd-click, native pointer all work; plain clicks
        // still route through the SPA below.
        cellHref={n =>
          n.id ? `/user/${n.id}`
          : n.team === 'communal' ? '/?l=communal'
          : n.team === 'unattributed' ? '/?l=unclaimed'
          : undefined}
        onCellClick={(n) => {
          if (redact) return true
          // Every tile goes somewhere sane: users to their page, the
          // ownerless pools to the matching home lens.
          if (n.id) navigate(`/user/${n.id}`)
          else if (n.team === 'communal') navigate('/?l=communal')
          else if (n.team === 'unattributed') navigate('/?l=unclaimed')
          return true
        }}
      />
    </div>
  )
}

/** KLC keep/sweep decomposition index for the loaded tree + live marks. */
function useKlcIdx(tree: TreeNode | undefined, idx: MarkIndex): KlcIndex | undefined {
  return useMemo(() => (tree && idx.count ? klcSplits(tree, idx.keeps) : undefined), [tree, idx])
}

/** `/users/og` — fixed 1200×630 unfurl render of the owner map: names + fate
 * stripes only (no sizes, no $, no tooltips). Screenshot via `pnpm shots`. */
export function UsersOgPage() {
  const asof = useLatestScan()
  const metaQ = useScanFile<Meta>('meta', asof)
  const treeQ = useScanFile<TreeNode>('tree', asof)
  const marksQ = useMarks(true)
  const idx = useMarkIndex(marksQ.data)
  const klcIdx = useKlcIdx(treeQ.data, idx)
  const fates = useMemo(
    () => (treeQ.data ? allUserFates(treeQ.data, idx, klcIdx, canonId) : null),
    [treeQ.data, idx, klcIdx],
  )
  useEffect(() => {
    const prev = document.documentElement.dataset.theme
    document.documentElement.dataset.theme = 'dark'
    return () => {
      if (prev) document.documentElement.dataset.theme = prev
      else delete document.documentElement.dataset.theme
    }
  }, [])
  return (
    <div className="og og-users">
      <div className="og-head">
        <h1>Marin GCS usage — users</h1>
        <p>Who owns what, and where every user’s bytes stand: keep / sweep / undecided.</p>
      </div>
      <div className="og-map">
        {metaQ.data && fates && <UsersMap meta={metaQ.data} fates={fates} redact />}
      </div>
      {metaQ.data && <MapLegend cells={ownerCells(metaQ.data, fates)} fates={fates} />}
    </div>
  )
}

export function UsersPage() {
  const asof = useLatestScan()
  const metaQ = useScanFile<Meta>('meta', asof)
  const treeQ = useScanFile<TreeNode>('tree', asof)
  const marksQ = useMarks(true)
  const idx = useMarkIndex(marksQ.data)
  const klcIdx = useKlcIdx(treeQ.data, idx)
  const mixes = metaQ.data?.user_class_bytes
  const fates = useMemo(
    () => (treeQ.data ? allUserFates(treeQ.data, idx, klcIdx, canonId) : null),
    [treeQ.data, idx, klcIdx],
  )
  // ONE basis for every column: once the fate walk has run, Attributed is
  // its claims-applied total (same numbers as the tiles, the fate columns,
  // and the CSV) — a scan-only Attributed next to walk-based Keep let a
  // user's keep exceed their "attributed" (Percy caught Michael at 67>41:
  // his claims added 40 Ti the old column ignored). Scan meta is only the
  // pre-load fallback.
  const users = useMemo(() => {
    const metaUsers = metaQ.data?.users ?? []
    if (!fates) return [...metaUsers].sort((a, b) => b.b - a.b)
    return [...fates.entries()]
      .map(([u, f]) => ({
        u,
        t: teamOf(u) ?? metaUsers.find(m => m.u === u)?.t ?? 'unknown',
        b: FATE_ORDER_TOTAL(f),
      }))
      .filter(x => x.b > 1e9)
      .sort((a, b) => b.b - a.b)
  }, [metaQ.data, fates])
  // Client-side CSV of exactly what the table shows (claims applied).
  const downloadCsv = () => {
    const rowsIter = fates
      ? [...fates.entries()].map(([u, f]) => ({ u, b: FATE_ORDER_TOTAL(f), f: foldFates(f) }))
      : users.map(u => ({ u: u.u, b: u.b, f: null as Record<ShownFate, number> | null }))
    const tib = 1024 ** 4
    const lines = [
      ['user', 'group', 'attributed_TiB', 'est_usd_mo', 'keep_TiB', 'sweep_TiB', 'undecided_TiB', 'undecided_pct', 'page'],
      ...rowsIter
        .filter(r => r.b > 1e9)
        .sort((a, b) => (b.f?.unmarked ?? b.b) - (a.f?.unmarked ?? a.b))
        .map(r => [
          r.u,
          teamOf(r.u) ?? users.find(m => m.u === r.u)?.t ?? 'unknown',
          (r.b / tib).toFixed(1),
          mixes?.[r.u] ? Math.round(ratePerByte(mixes[r.u]) * r.b) : '',
          ((r.f?.keep ?? 0) / tib).toFixed(1),
          ((r.f?.sweep ?? 0) / tib).toFixed(1),
          ((r.f?.unmarked ?? r.b) / tib).toFixed(1),
          r.b ? Math.round((100 * (r.f?.unmarked ?? r.b)) / r.b) : 0,
          `https://gcs.oa.dev/user/${r.u}`,
        ]),
    ]
    const blob = new Blob([lines.map(l => l.join(',')).join('\n') + '\n'], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `marin-gcs-mark-status-${asof ?? 'latest'}.csv`
    a.click()
    URL.revokeObjectURL(a.href)
  }
  const cell = (u: string, f: ShownFate) => {
    const raw = fates?.get(u)
    const b = raw ? foldFates(raw)[f] : 0
    return (
      <td className="num" style={b ? { color: fateColor(f) } : { color: 'var(--ink-2)', opacity: 0.5 }}>
        {fates ? (b ? fmtBytesIec(b) : '—') : '…'}
      </td>
    )
  }
  // Footer totals over exactly the rows shown (same claims-applied basis);
  // $ only sums users whose class mix is known, so it's a floor, flagged as such.
  const totals = useMemo(() => {
    const t = { b: 0, usd: 0, priced: 0, keep: 0, sweep: 0, unmarked: 0 }
    for (const u of users) {
      t.b += u.b
      const mix = mixes?.[u.u]
      if (mix) { t.usd += ratePerByte(mix) * u.b; t.priced++ }
      const raw = fates?.get(u.u)
      if (raw) { const f = foldFates(raw); t.keep += f.keep; t.sweep += f.sweep; t.unmarked += f.unmarked }
    }
    return t
  }, [users, mixes, fates])
  const totalCell = (f: ShownFate) => (
    <td className="num" style={{ color: fateColor(f) }}>{fates ? fmtBytesIec(totals[f]) : '…'}</td>
  )
  return (
    <main className="marks-page user-page">
      <header>
        <div className="hrow">
          <h1>Users</h1>
          <span style={{ display: 'inline-flex', gap: '1.2em', alignItems: 'baseline' }}>
            <button type="button" className="csv-btn" onClick={downloadCsv}>Download&nbsp;CSV</button>
            <a className="nav-files" href={SHEET_URL} target="_blank" rel="noreferrer">Google&nbsp;Sheet&nbsp;↗</a>
            <Link className="nav-files" to="/" style={{ fontSize: '0.9em' }}>←&nbsp;Home</Link>
          </span>
        </div>
        <p className="sub">Everyone with attributed storage{asof && <> in the {asof} scan</>}, largest first — and where their bytes stand (keep / sweep / no decision yet). Click a user (row or tile) for the per-prefix breakdown.</p>
      </header>
      {metaQ.isLoading && <p className="loading">loading…</p>}
      {metaQ.data && (
        <>
          <UsersMap meta={metaQ.data} fates={fates} />
          <MapLegend cells={ownerCells(metaQ.data, fates)} fates={fates} />
        </>
      )}
      {users.length > 0 && (
        <table className="worklist">
          <thead>
            <tr>
              <th>User</th><th className="num">Attributed</th><th className="num">Est. $/mo</th>
              <th className="num">Keep</th><th className="num">Sweep</th><th className="num">Unmarked</th>
            </tr>
          </thead>
          <tbody>
            {users.map(u => (
              <tr key={u.u}>
                <td className="user-td">
                  <Link className="user-link" to={`/user/${u.u}`}>
                    <UserChip who={u.u} before={<GroupBadge team={teamOf(u.u) ?? u.t} tip={false} />} />
                  </Link>
                </td>
                <td className="num">{fmtBytesIec(u.b)}</td>
                <td className="num"><DollarCell b={u.b} mix={mixes?.[u.u]} /></td>
                {cell(u.u, 'keep')}
                {cell(u.u, 'sweep')}
                {cell(u.u, 'unmarked')}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="total-row">
              <td>Total <span style={{ fontWeight: 400, opacity: 0.7 }}>· {users.length} users</span></td>
              <td className="num">{fmtBytesIec(totals.b)}</td>
              <td className="num">
                {totals.priced
                  ? <Tooltip content={totals.priced < users.length ? `${totals.priced} of ${users.length} users have a storage-class mix; the rest are unpriced, so this is a floor` : 'sum of the per-user estimates'}>
                      <span className="has-tt">{totals.priced < users.length ? '≥ ' : ''}{fmtUsd(totals.usd)}</span>
                    </Tooltip>
                  : '—'}
              </td>
              {totalCell('keep')}
              {totalCell('sweep')}
              {totalCell('unmarked')}
            </tr>
          </tfoot>
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
          <tr><th>Mark</th><th className="num">Your data</th><th>Prefix</th><th>By</th><th>When</th></tr>
        </thead>
        <tbody>
          {slice.map(r => (
            <tr key={r.uri}>
              <td>
                <span className="chip" style={{ borderColor: fateColor(r.fate), color: fateColor(r.fate) }}>
                  {fateLabel(r.fate)}
                </span>
              </td>
              <td className="num">{fmtBytesIec(r.b)}</td>
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

/** `/user/:id/og` — fixed 1200×630 per-user unfurl card: avatar, name, group
 * glyph, and the keep / sweep / undecided proportions (percentages only —
 * no bytes, no $). Screenshot by `scripts/shoot-user-ogs.mjs`. */
export function UserOgPage() {
  const { id = '' } = useParams()
  const asof = useLatestScan()
  const treeQ = useScanFile<TreeNode>('tree', asof)
  const marksQ = useMarks(true)
  const idx = useMarkIndex(marksQ.data)
  const klcIdx = useKlcIdx(treeQ.data, idx)
  const fates = useMemo(
    () => (treeQ.data ? allUserFates(treeQ.data, idx, klcIdx, canonId) : null),
    [treeQ.data, idx, klcIdx],
  )
  useEffect(() => {
    const prev = document.documentElement.dataset.theme
    document.documentElement.dataset.theme = 'dark'
    return () => {
      if (prev) document.documentElement.dataset.theme = prev
      else delete document.documentElement.dataset.theme
    }
  }, [])
  const raw = fates?.get(id)
  const f = raw ? foldFates(raw) : null
  const total = f ? SHOWN_FATES.reduce((s, k) => s + f[k], 0) : 0
  const team = teamOf(id)
  const pct = (k: ShownFate): number => (f && total ? (100 * f[k]) / total : 0)
  const barColor = (k: ShownFate): string => (k === 'unmarked' ? 'var(--other)' : fateColor(k))
  const barLabel: Record<ShownFate, string> = { keep: 'keep', sweep: 'sweep', unmarked: 'undecided' }
  return (
    <div className="og og-user">
      <div className="og-head ogu-head">
        <Avatar github={ghHandle(id)} name={shortName(id)} size={110} />
        <div>
          <h1>
            {shortName(id)}
            {team && GROUP_ICONS[team] && <img className="ogu-glyph" src={GROUP_ICONS[team]} alt={GROUP_LABELS[team] ?? team} />}
          </h1>
          <p>Marin GCS usage — where their bytes stand.</p>
        </div>
      </div>
      {f && total > 0 && (
        <>
          <div className="ogu-bar">
            {SHOWN_FATES.filter(k => f[k] > 0).map(k => (
              <div key={k} style={{ width: `${pct(k)}%`, background: barColor(k) }} />
            ))}
          </div>
          <div className="ogu-legend">
            {SHOWN_FATES.filter(k => f[k] > 0).map(k => (
              <span key={k}>
                <i style={{ background: barColor(k) }} />
                {barLabel[k]} <b>{Math.round(pct(k))}%</b>
              </span>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

export function UserPage() {
  const { id = '' } = useParams()
  const asof = useLatestScan()
  const treeQ = useScanFile<TreeNode>('tree', asof)
  const metaQ = useScanFile<Meta>('meta', asof)
  const marksQ = useMarks(true)
  const idx = useMarkIndex(marksQ.data)
  const klcIdx = useKlcIdx(treeQ.data, idx)
  // Scan attribution + live-claim overlay ("committed + WAL") — the page's
  // headline totals come from here, so a claim reshapes them immediately.
  const fatesAll = useMemo(
    () => (treeQ.data ? allUserFates(treeQ.data, idx, klcIdx, canonId) : null),
    [treeQ.data, idx, klcIdx],
  )
  const mine = fatesAll?.get(id) ?? null
  // Drill state lives in `?p=` so deep views are shareable and the back
  // button walks back out (same contract as the homepage map).
  const [pP, setPP] = useUrlState('p', stringParam())
  // The user's slice of the estate as a drillable map (same scoping as the
  // homepage's "My files" lens: maximal subtrees ≥60% theirs), colored by
  // mark state.
  const scopedTree = useMemo(() => {
    if (!treeQ.data) return null
    const base = applyNodeFilter(treeQ.data, lensNodePred(userLens(id)))
    if (base.b > 0) return base
    // No scan-attributed subtrees (fresh identity / claims-only): scope the
    // map to the claimed prefixes instead.
    const claims = new Set(
      [...idx.owners.values()]
        .filter(r => r.owner != null && canonId(r.owner) === id)
        .map(r => prefixToPath(r.prefix)),
    )
    if (!claims.size) return null
    const claimed = applyFilter(treeQ.data, path => claims.has(path))
    return claimed.b > 0 ? claimed : null
  }, [treeQ.data, id, idx])

  const rows = useMemo(
    () => (treeQ.data ? userFates(treeQ.data, id, idx) : []),
    [treeQ.data, id, idx],
  )
  const totals = useMemo(() => {
    const t = new Map<ShownFate, { b: number; n: number }>(SHOWN_FATES.map(f => [f, { b: 0, n: 0 }]))
    for (const r of rows) {
      // KLC rows split into their real keep/sweep proportions (the prefix
      // itself counts under keep); unresolvable KLC counts whole as keep.
      if (r.fate === 'keep_last_ckpt' && klcIdx && r.mark) {
        const pfx = r.mark.prefix.endsWith('/') ? r.mark.prefix : r.mark.prefix + '/'
        const split = klcIdx.get(pfx)
        if (split && split.totalB > 0) {
          const rel = klcFateAt(r.uri, split)
          const ratio = rel === 'keep' ? 1 : rel === 'sweep' ? 0 : Math.min(1, klcKeptWithin(r.uri, split) / split.totalB)
          const keep = t.get('keep')!
          keep.b += r.b * ratio
          keep.n++
          t.get('sweep')!.b += r.b * (1 - ratio)
          continue
        }
      }
      const cur = t.get(r.fate === 'keep_last_ckpt' ? 'keep' : r.fate)!
      cur.b += r.b
      cur.n++
    }
    return t
  }, [rows, klcIdx])
  const attributed = mine ? FATE_ORDER_TOTAL(mine) : rows.reduce((s, r) => s + r.b, 0)
  const stripBytes = mine ? foldFates(mine) : null
  const metaB = metaQ.data?.users?.find(u => u.u === id)?.b
  const mix = metaQ.data?.user_class_bytes?.[id]
  const authored = useMemo(
    () => new Set((marksQ.data?.keeps ?? []).filter(r => r.keep != null && canonId(r.who) === id).map(r => r.prefix)).size,
    [marksQ.data, id],
  )
  // Fallback content for a user with ledger activity but no attributed bytes
  // yet (fresh identity, or claims the attribution pipeline hasn't mapped):
  // their own latest live marks, sized from the tree (each prefix's total
  // bytes — 0 when it sits below the tree's floors).
  const authoredRows = useMemo((): FateRow[] => {
    if (rows.length > 0 || !treeQ.data) return []
    const root = treeQ.data
    const nodeAt = (prefix: string): TreeNode | undefined => {
      let node: TreeNode | undefined = root
      for (const s of prefix.replace(/^[a-z0-9]+:\/\//, '').replace(/\/+$/, '').split('/')) {
        node = node?.c?.find(c => c.n === s)
      }
      return node
    }
    const out: FateRow[] = []
    for (const r of idx.keeps.values()) {
      if (r.keep == null || canonId(r.who) !== id) continue
      out.push({
        uri: r.prefix,
        fate: r.keep,
        b: nodeAt(r.prefix)?.b ?? 0,
        mark: { prefix: r.prefix, action: r.keep, who: r.who, ts: r.ts, note: r.memo },
      })
    }
    return out.sort((a, b) => b.b - a.b)
  }, [rows.length, treeQ.data, idx, id])
  const claimedRows = useMemo((): FateRow[] => {
    if (!treeQ.data) return []
    const root = treeQ.data
    const nodeAt = (prefix: string): TreeNode | undefined => {
      let node: TreeNode | undefined = root
      for (const s of prefix.replace(/^[a-z0-9]+:\/\//, '').replace(/\/+$/, '').split('/')) {
        node = node?.c?.find(c => c.n === s)
      }
      return node
    }
    const out: FateRow[] = []
    for (const r of idx.owners.values()) {
      if (r.owner == null || canonId(r.owner) !== id) continue
      const st = idx.resolve(r.prefix)
      out.push({ uri: r.prefix, fate: st.mark?.action ?? 'unmarked', b: nodeAt(r.prefix)?.b ?? 0, mark: st.mark })
    }
    return out.sort((a, b) => b.b - a.b)
  }, [treeQ.data, idx, id])
  const claimed = claimedRows.length
  const decidedRows = rows.filter(r => r.fate !== 'unmarked')
  const undecidedRows = rows.filter(r => r.fate === 'unmarked')
  // Resolve `?p=` against the scoped tree each render; a vanished segment
  // truncates to its deepest surviving ancestor.
  const mapPath = useMemo((): TreeNode[] | undefined => {
    if (!scopedTree) return undefined
    const path = [scopedTree]
    let cur: TreeNode = scopedTree
    for (const s of (pP ?? '').split('/').filter(Boolean)) {
      const next = cur.c?.find(c => c.n === s)
      if (!next) break
      path.push(next)
      cur = next
    }
    return path
  }, [scopedTree, pP])
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
            <Link className="nav-files" to={`/?l=user&lu=${shortUserKey(id)}`} style={{ fontSize: '0.9em' }}>Home,&nbsp;filtered&nbsp;to&nbsp;{shortName(id)}&nbsp;→</Link>
            <Link className="nav-files" to="/users" style={{ fontSize: '0.9em' }}>All&nbsp;users</Link>
            <Link className="nav-files" to="/" style={{ fontSize: '0.9em' }}>←&nbsp;Home</Link>
          </span>
        </div>
        <p className="sub">
          {fmtBytesIec(attributed, true)} attributed ({asof ?? '…'} scan + live claims)
          {mix != null && metaB != null && <> · est. {fmtUsd(ratePerByte(mix) * metaB)}/mo</>}
          {authored > 0 && <> · {fmtN(authored)} prefixes marked by {shortName(id)}</>}.
          Where every byte stands, resolved the way the map does it (most recent mark on an ancestor-or-equal prefix wins).
        </p>
      </header>

      {loading && <p className="loading">loading…</p>}
      {marksQ.error && <p className="tab-note" style={{ color: 'var(--s3)' }}>Couldn’t load marks: {marksQ.error.message}</p>}
      {!loading && !rows.length && attributed === 0 && (
        <>
          <p className="tab-note">
            No attributed or claimed data for “{id}”
            {authoredRows.length > 0 ? <>{' '}— but their marks are below.</> : '.'}
          </p>
          {authoredRows.length > 0 && (
            <>
              <h2>Marked by {shortName(id)}</h2>
              <p className="tab-note">Their keep / sweep decisions (sizes are each prefix’s total bytes, whoever owns them).</p>
              <FateTable rows={authoredRows} empty="No marks yet." />
            </>
          )}
        </>
      )}

      {(rows.length > 0 || attributed > 0) && (
        <>
          <div className="fate-strip">
            {SHOWN_FATES.map(f => {
              const { b: rowB, n } = totals.get(f)!
              const b = stripBytes ? stripBytes[f] : rowB
              if (!b) return null
              return (
                <div className="fate-cell" key={f} style={{ borderColor: fateColor(f) }}>
                  <b style={{ color: fateColor(f) }}>{fateLabel(f)}</b>
                  <span className="fate-b">{fmtBytesIec(b)}</span>
                  <span className="fate-n">{n > 0 && <>{fmtN(n)} prefix{n === 1 ? '' : 'es'} · </>}{attributed ? Math.round((b / attributed) * 100) : 0}%</span>
                </div>
              )
            })}
          </div>

          {scopedTree && scopedTree.b > 0 && (
            <div className="user-mini-map">
              <MarkTreemap
                root={scopedTree}
                mode="fate"
                userIdx={new Map()}
                dateRange={null}
                scheme={store.scheme}
                markIdx={idx}
                klcIdx={klcIdx}
                path={mapPath}
                onPathChange={pth => setPP(pth.slice(1).map(n => n.n).join('/') || undefined)}
              />
            </div>
          )}

          {rows.length > 0 && (
            <>
              <h2>Decided</h2>
              <p className="tab-note">Prefixes whose mark governs your bytes — yours and anyone else’s marks both count.</p>
              <FateTable rows={decidedRows} empty="Nothing marked yet." />

              <h2>Undecided</h2>
              <p className="tab-note">Your largest subtrees with no keep / sweep decision anywhere above or below — swept by default once the review window closes (date TBD).</p>
              <FateTable rows={undecidedRows} empty="Every attributed byte has a decision. 🎉" />
            </>
          )}

          {claimedRows.length > 0 && (
            <>
              <h2>Claimed</h2>
              <p className="tab-note">Prefixes claimed in the ledger — counted in the totals above immediately; the scan pipeline formalizes the attribution on its next run.</p>
              <FateTable rows={claimedRows} empty="" />
            </>
          )}
        </>
      )}
    </main>
  )
}
