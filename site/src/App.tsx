import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdLayers, MdLightMode } from 'react-icons/md'
import { HotkeysProvider, Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AgeChart } from './AgeChart'
import { useCanMark, useIdent as useIdentity, useSignOut } from './auth'
import TokenModal from './TokenModal'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap } from './DiffTreemap'
import type { DiffData } from './DiffTreemap'
import { buildUserIndex, epochDaysToDate } from './colors'
import { ChildrenTable } from './ChildrenTable'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
import { applyFilter, applyNodeFilter, parseQuery } from './filterTree'
import { setCurrentScan, useMarkIndex, useMarks, sweepDaysLeft } from './marks'
import { MarkTabs } from './MarkTabs'
import type { MarkTab } from './MarkTabs'
import { lensNodePred, teamLens, useMyUser, userLens } from './sweep'
import { STORES, storeForPath } from './stores'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_NAMES, CLASS_PRICE_US, MODE_LABELS, fmtN, ratePerByte } from './types'
import { useUnits } from './units'
const MODES = Object.keys(MODE_LABELS) as ColorMode[]
const REPO_URL = 'https://github.com/Open-Athena/marin-gcs-usage'
// How often an unpinned tab re-checks for newly published scans.
const SCANS_POLL_MS = 5 * 60_000

// Scan labels: drop the redundant year for the current one, so a list of
// same-year scans reads as `8/17` rather than `2026-08-17`. Scan ids are
// `YYYY-MM-DD`, optionally sub-daily as `YYYY-MM-DDTHHMM`.
export function fmtScan(s: string, now = new Date()): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):?(\d{2}))?/.exec(s)
  if (!m) return s
  const [, y, mo, d, hh, mm] = m
  if (!hh) {
    // Date-only ids (the daily GCS job) are calendar dates, not instants —
    // rendering them through a timezone would shift some readers a day off.
    return Number(y) === now.getFullYear() ? `${Number(mo)}/${Number(d)}` : `${y}-${mo}-${d}`
  }
  // Sub-daily ids are UTC instants; display in the viewer's local time,
  // 12-hour with a bare a/p ("8/19 6:08a"). The `?d=` token stays UTC (see
  // decodeScan) — display converts, the URL doesn't.
  const dt = new Date(Date.UTC(+y, +mo - 1, +d, +hh, +(mm ?? '0')))
  const h = dt.getHours()
  const time = `${h % 12 || 12}:${String(dt.getMinutes()).padStart(2, '0')}${h < 12 ? 'a' : 'p'}`
  const md = `${dt.getMonth() + 1}/${dt.getDate()}`
  return dt.getFullYear() === now.getFullYear()
    ? `${md} ${time}`
    : `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ${time}`
}

// `?d` is a *prefix* of a scan id (always UTC), accepted in several spellings —
// examples that all pin the 2026-08-19T1008 scan:
//   260819-1008 · 260819T10 (compact date; `-` or `T` before the time)
//   8-19-10 · 8-19-10-08    (M-D-H[-MM]; current year assumed)
//   26-8-19-10 · 2026-8-19-10 (year-first when the lead component can't be a month)
// The canonical/emitted form stays compact (`260819-1008`). A token matching
// several scans renders the newest plus a disambiguation strip listing the rest.
export const encodeScan = (v: string | undefined): string | undefined => {
  const m = v && /^\d{2}(\d{2})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?$/.exec(v)
  if (!m) return v || undefined
  const [, y, mo, d, hh, mm] = m
  return `${y}${mo}${d}` + (hh ? `-${hh}${mm ?? ''}` : '')
}

export const decodeScan = (e: string | undefined, now = new Date()): string | undefined => {
  if (!e) return undefined
  const pad = (n: string) => n.padStart(2, '0')
  const compact = /^(\d{2})(\d{2})(\d{2})(?:[T-](\d{2})(\d{2})?)?$/.exec(e)
  if (compact) {
    const [, y, mo, d, hh, mm] = compact
    return `20${y}-${mo}-${d}` + (hh ? `T${hh}${mm ?? ''}` : '')
  }
  const parts = e.split(/[T-]/)
  if (parts.length < 2 || parts.length > 5 || parts.some(p => !/^(\d{1,2}|\d{4})$/.test(p))) return undefined
  // A lead component that can't be a month is a year (2- or 4-digit); 4-digit
  // components anywhere else are malformed.
  const yearFirst = parts[0].length === 4 || Number(parts[0]) > 12
  if (parts.slice(yearFirst ? 1 : 0).some(p => p.length > 2)) return undefined
  const y = yearFirst ? (parts[0].length === 4 ? parts[0] : `20${parts[0]}`) : String(now.getUTCFullYear())
  const [mo, d, hh, mm] = parts.slice(yearFirst ? 1 : 0)
  if (!mo || !d || Number(mo) < 1 || Number(mo) > 12 || Number(d) < 1 || Number(d) > 31) return undefined
  if ((hh && Number(hh) > 23) || (mm && Number(mm) > 59)) return undefined
  return `${y}-${pad(mo)}-${pad(d)}` + (hh ? `T${pad(hh)}${mm ? pad(mm) : ''}` : '')
}

const avatarHue = (s: string): number => {
  let h = 0
  for (const c of s) h = (h * 31 + c.codePointAt(0)!) % 360
  return h
}

type Theme = 'system' | 'dark' | 'light'
const THEME_KEY = 'gcs-usage:theme'

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem(THEME_KEY) as Theme) || 'system')
  useEffect(() => {
    if (theme === 'system') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = theme
    localStorage.setItem(THEME_KEY, theme)
  }, [theme])
  return [theme, () => setTheme(t => (t === 'system' ? 'dark' : t === 'dark' ? 'light' : 'system'))]
}

function AppContent() {
  // Which object store to render comes from the path (`/` = GCS, `/cw` = the
  // CoreWeave bucket), so each store is its own shareable URL with its own
  // unfurl card rather than a query param on a single page.
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const store = storeForPath(pathname)
  // Mark & sweep mode (specs/mark-sweep-ui.md): same treemap, plus keep/delete
  // badges and marking controls. GCS only — CoreWeave is out of the sweep.
  const markMode = pathname === '/mark' && store.key === 'gcs'
  const marksQ = useMarks(markMode)
  const markIdx = useMarkIndex(marksQ.data)
  // The shell's <title> is rewritten per store for crawlers (functions/cw/), but
  // client-side navigation between stores has to keep the tab in sync too.
  useEffect(() => {
    document.title = store.title
  }, [store])
  // URL token matches the visible label ("age"), not the internal key ("date")
  const [modeP, setModeP] = useUrlState('c', {
    encode: (v: string | undefined) => (v === 'team' || v === undefined ? undefined : v === 'date' ? 'age' : v),
    decode: (e: string | undefined) => (e === undefined ? 'team' : e === 'age' ? 'date' : e),
  })
  const [hlUser, setHlUser] = useUrlState('u', stringParam())
  const [hlTeam, setHlTeam] = useUrlState('t', stringParam())
  // Selected scan in the URL as short YYMMDD (`?d=260809`). Absent is a
  // first-class state meaning "latest", so a tab parked on gcs.oa.dev keeps
  // following new scans instead of pinning whatever day it was opened. `?d=`
  // appears only when a specific scan is chosen — digest deep-links still
  // resolve, they just aren't manufactured for the default view.
  const [dP, setDP] = useUrlState('d', { encode: encodeScan, decode: decodeScan })
  // The scan list polls so an unpinned tab discovers new scans on its own; the
  // per-scan payloads are immutable once published, so they never refetch.
  // Every key is store-scoped, so switching stores swaps the whole payload set
  // rather than mixing one store's tree with another's scan list.
  const scansQ = useQuery<string[]>({
    queryKey: ['scans', store.key],
    queryFn: () => fetch(`${store.base}/scans.json`).then(r => r.json()),
    refetchInterval: SCANS_POLL_MS,
  })
  const rulesQ = useQuery({
    queryKey: ['rules'],
    queryFn: () => fetch('/data/rules.json').then(r => r.json()),
    retry: false,
  })
  const scans = useMemo(() => scansQ.data ?? [], [scansQ.data])
  const rules: Rules | null = rulesQ.data ?? null
  // `scans` is newest-first, so the first prefix match is the newest one.
  const dMatches = useMemo(() => (dP ? scans.filter(s => s.startsWith(dP)) : []), [dP, scans])
  const asof = dMatches[0] ?? scans[0] ?? null
  // Ledger actions record which scan the actor was viewing.
  useEffect(() => setCurrentScan(asof ?? undefined), [asof])
  const scanQuery = <T,>(name: string) => ({
    queryKey: [name, store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
  const treeQ = useQuery(scanQuery<TreeNode>('tree'))
  const ageQ = useQuery(scanQuery<AgeRow[]>('age'))
  const metaQ = useQuery(scanQuery<Meta>('meta'))
  // Optional: precomputed diff vs the previous snapshot (job/cw-diff.py).
  // Older snapshots (and the GCS store, for now) don't have one — a 404 just
  // hides the section, so no retry storm.
  const diffQ = useQuery<DiffData | null>({
    queryKey: ['diff', store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/diff.json`).then(r => (r.ok ? r.json() : null)),
    enabled: !!asof,
    staleTime: Infinity,
    retry: false,
  })
  const diff: DiffData | null = diffQ.data ?? null
  const tree: TreeNode | null = treeQ.data ?? null
  // Name filter (`?f=`): substring or /regex/ over path segments, outermost
  // match keeps its subtree, ancestors re-aggregate to matched bytes only.
  // In-memory over the loaded tree — sub-depth-cap filtering is the
  // vocab-sidecar arc and lands later.
  const [fq, setFq] = useUrlState('f', stringParam())
  const pred = useMemo(() => (fq ? parseQuery(fq) : null), [fq])
  const shownTree = useMemo(() => (tree && pred ? applyFilter(tree, pred) : tree), [tree, pred])
  const age: AgeRow[] = ageQ.data ?? []
  const meta: Meta | null = metaQ.data ?? null
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const { units, suffixB, fmtBytes, toggleUnits, toggleSuffixB } = useUnits()
  const [theme, cycleTheme] = useTheme()
  const ident = useIdentity()
  const signOut = useSignOut()
  const canMark = useCanMark()
  const [tokenOpen, setTokenOpen] = useState(false)
  const myUser = useMyUser(ident?.email, markMode)
  // `?mt=` — active /mark tab (absent = "mine").
  const [markTabP, setMarkTabP] = useUrlState('mt', stringParam())
  const markTab: MarkTab =
    markTabP === 'todo' || markTabP === 'lost' || markTabP === 'communal' || markTabP === 'all' ? markTabP : 'mine'
  const setMarkTab = (t: MarkTab) => setMarkTabP(t === 'mine' ? undefined : t)
  // `?mu=` — whose files the "mine" tab shows (anyone's view is browsable).
  const [muP, setMuP] = useUrlState('mu', stringParam())
  const viewUser = muP ?? myUser
  // `?ms=0` — scope-map-to-view toggled off (on is the default).
  const [scopedP, setScopedP] = useUrlState('ms', stringParam())
  const scoped = scopedP !== '0'
  const setScoped = (v: boolean) => setScopedP(v ? undefined : '0')
  // `?p=` — the treemap's drill path (slash-joined segments below the root).
  const [pP, setPP] = useUrlState('p', stringParam())
  // Read-recency lens domain: the access-log observation window (meta), not
  // the tree's own min/max — "no reads" is only meaningful vs when logging began.
  const readRange = useMemo((): DateRange | null =>
    meta?.access ? { min: meta.access.from, max: meta.access.to } : null,
  [meta])
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : 'team'
  const setMode = (m: ColorMode) => setModeP(m)
  const hasAttr = !!tree?.tm
  const effMode: ColorMode = mode === 'read' && !readRange ? 'team' : hasAttr ? mode : 'tree'
  const hl: Highlight | null = hlUser ? { user: hlUser } : hlTeam ? { team: hlTeam } : null
  // In mark mode the active tab scopes the map to its lens (filter +
  // re-aggregate — the worklists keep the unscoped tree, so their
  // maximal-subtree rows don't coarsen); untoggled, fall back to dimming.
  // `todo` is cross-cutting (no single lens), so it never scopes the map.
  const scopedActive = markMode && scoped && markTab !== 'all' && markTab !== 'todo' && (markTab !== 'mine' || !!viewUser)
  const mapTree = useMemo(() => {
    if (!shownTree || !scopedActive) return shownTree
    const lens = markTab === 'mine'
      ? userLens(viewUser!)
      : teamLens(markTab === 'lost' ? 'unattributed' : 'communal')
    return applyNodeFilter(shownTree, lensNodePred(lens))
  }, [shownTree, scopedActive, markTab, viewUser])
  // Controlled treemap drill path, resolved against the (possibly filtered/
  // scoped) tree each render: `?p=` survives scope toggles, filters, and scan
  // switches by re-walking the new tree; a vanished path truncates to its
  // deepest surviving ancestor.
  const mapPath = useMemo((): TreeNode[] | undefined => {
    if (!mapTree) return undefined
    const path = [mapTree]
    let cur: TreeNode = mapTree
    for (const s of (pP ?? '').split('/').filter(Boolean)) {
      const next = cur.c?.find(c => c.n === s)
      if (!next) break
      path.push(next)
      cur = next
    }
    // A store with one bucket (CoreWeave today) opens inside it — the bucket
    // level is a single full-width box otherwise.
    if (path.length === 1 && mapTree.c?.length === 1) return [mapTree, mapTree.c[0]]
    return path
  }, [mapTree, pP])
  const onMapPath = (p: TreeNode[]) =>
    setPP(p.length > 1 ? p.slice(1).map(n => n.n).join('/') : undefined)
  // Worklist rows / children table → drill the map to a prefix and show it.
  const openPath = (segs: string[]) => {
    setPP(segs.length ? segs.join('/') : undefined)
    document.querySelector('.dt-treemap')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  const effHl: Highlight | null = !markMode
    ? hl
    : scopedActive ? null
    : markTab === 'mine' && viewUser ? { user: viewUser }
    : markTab === 'lost' ? { team: 'unattributed' }
    : markTab === 'communal' ? { team: 'communal' }
    : null

  // The prod hostnames are one-store-each; localhost/previews serve both.
  const crossSite = useMemo(() => {
    const host = location.hostname
    if (/^cw[-.]/.test(host)) return { label: 'GCS usage', href: 'https://gcs.oa.dev/' }
    if (host === 'gcs.oa.dev') return { label: 'CoreWeave usage', href: 'https://cw-s3.oa.dev/' }
    return null
  }, [])

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])
  const mkUsers = useMemo(() => (meta?.users ?? []).map(u => u.u).sort(), [meta])

  const pickUser = (u: string) => {
    setHlTeam(undefined)
    setHlUser(u)
    if (mode !== 'user' && mode !== 'uteam') setMode('user')
  }
  const pickTeam = (t: string) => {
    setHlUser(undefined)
    setHlTeam(t)
    if (mode !== 'team') setMode('team')
  }
  const clearHl = () => {
    setHlUser(undefined)
    setHlTeam(undefined)
  }

  useActions({
    ...Object.fromEntries(
      MODES.map((m, i) => [
        `mode:${m}`,
        {
          label: `Color by ${MODE_LABELS[m]}`,
          group: 'Color mode',
          defaultBindings: [String(i + 1)],
          handler: () => setMode(m),
        },
      ]),
    ),
    'highlight:clear': {
      label: 'Clear user/group highlight',
      group: 'Highlight',
      defaultBindings: ['x'],
      handler: clearHl,
    },
    'theme:cycle': {
      label: `Theme: ${theme} (cycle)`,
      group: 'View',
      defaultBindings: ['shift+d'],
      handler: cycleTheme,
    },
    'lens:classes': {
      label: 'Storage-class lens (hatch colder-class bytes)',
      group: 'View',
      defaultBindings: ['s'],
      handler: () => setLens(v => !v),
    },
    'units:toggle': {
      label: `Byte units: ${units === 'si' ? 'SI (TB) → IEC (TiB)' : 'IEC (TiB) → SI (TB)'}`,
      group: 'View',
      defaultBindings: ['i'],
      handler: toggleUnits,
    },
    'units:suffix': {
      label: `Unit suffix: ${suffixB ? 'TiB/TB → Ti/T (drop B)' : 'Ti/T → TiB/TB (show B)'}`,
      group: 'View',
      defaultBindings: ['B'],
      handler: toggleSuffixB,
    },
    ...Object.fromEntries(
      (meta?.users ?? []).map(u => [
        `user:${u.u}`,
        {
          label: `${u.u} · ${u.t} · ${fmtBytes(u.b)}`,
          group: 'Users',
          handler: () => pickUser(u.u),
        },
      ]),
    ),
    ...Object.fromEntries(
      (rules?.teams ?? []).map(t => [
        `group:${t}`,
        {
          label: `group: ${t}`,
          group: 'Groups',
          handler: () => pickTeam(t),
        },
      ]),
    ),
    ...Object.fromEntries(
      scans.map(s => [
        `scan:${s}`,
        {
          label: `Scan ${fmtScan(s)}`,
          group: 'Scans',
          handler: () => setDP(s),
        },
      ]),
    ),
    ...Object.fromEntries(
      STORES.map(s => [
        `store:${s.key}`,
        {
          label: `Store: ${s.label}`,
          group: 'Stores',
          handler: () => navigate({ pathname: s.path, search }),
        },
      ]),
    ),
  })

  const dateRange = useMemo((): DateRange | null => {
    if (!tree) return null
    let min = Infinity
    let max = -Infinity
    const walk = (n: TreeNode) => {
      if (n.d != null && !n.c) {
        if (n.d < min) min = n.d
        if (n.d > max) max = n.d
      }
      n.c?.forEach(walk)
    }
    walk(tree)
    return min < max ? { min, max } : null
  }, [tree])

  const catOrder = useMemo(() => {
    if (!tree) return []
    const catBytes = new Map<string, number>()
    for (const bucket of tree.c ?? [])
      for (const d of bucket.c ?? []) {
        const k = d.n.startsWith('(') ? '(other)' : d.n
        catBytes.set(k, (catBytes.get(k) ?? 0) + d.b)
      }
    return [...catBytes.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k).filter(k => k !== '(other)')
  }, [tree])

  // $ figures are GCS list prices per storage class, so they're only meaningful
  // for stores that have those classes — a CoreWeave bucket priced at GCS rates
  // would be an invented number, so its cost UI is dropped rather than faked.
  const estCost = useMemo(() => {
    if (!meta || !store.prices) return null
    const gib = (b: number) => b / 1024 ** 3
    const list = Object.entries(meta.class_bytes ?? {}).reduce(
      (s, [c, b]) => s + gib(b) * (CLASS_PRICE_US[c] ?? 0.02),
      0,
    )
    return { list }
  }, [meta, store])

  const pricing = useMemo((): Pricing | null => {
    if (!meta || !store.prices) return null
    const rates = (m?: Record<string, Record<string, number>>) =>
      m && Object.fromEntries(Object.entries(m).map(([k, cb]) => [k, ratePerByte(cb)]))
    return {
      blended: ratePerByte(meta.class_bytes),
      teamRates: rates(meta.team_class_bytes),
      userRates: rates(meta.user_class_bytes),
      teamMix: meta.team_class_bytes,
      userMix: meta.user_class_bytes,
    }
  }, [meta, store])

  return (
    <main>
      {tokenOpen && <TokenModal onClose={() => setTokenOpen(false)} />}
      <header>
        <div className="hrow">
          <h1>{store.title}</h1>
          {/* On the prod hostnames each store is its own site (own Access
              audience), so the in-app switcher would be a nop or an auth
              surprise — cross-link instead, in a new tab. Localhost and
              pages.dev previews keep the chips for dev convenience. */}
          {crossSite ? (
            <a className="crosslink" href={crossSite.href} target="_blank" rel="noreferrer">
              {crossSite.label}&nbsp;↗
            </a>
          ) : STORES.length > 1 && (
            <div className="storectl" role="radiogroup" aria-label="Object store">
              {STORES.map(s => (
                <button
                  key={s.key}
                  role="radio"
                  aria-checked={store.key === s.key}
                  className={store.key === s.key ? 'on' : ''}
                  // keep the view params (color mode, scan, highlight) across stores
                  onClick={() => navigate({ pathname: s.path, search })}
                >
                  {s.label}
                </button>
              ))}
            </div>
          )}
          <Link className="nav-files" to="/files" style={{ fontSize: '0.9em' }}>Browse&nbsp;scans&nbsp;→</Link>
          {/* /mark stays URL-only until the Monday reveal — no nav link yet */}
          {markMode && <Link className="nav-files" to="/" style={{ fontSize: '0.9em' }}>←&nbsp;Exit&nbsp;marking</Link>}
          {ident && (
            <div className="whoami">
              <span className="avatar" style={{ background: `hsl(${avatarHue(ident.email)} 55% 42%)` }} title={ident.name || ident.email}>
                {(ident.name || ident.email).trim()[0].toUpperCase()}
              </span>
              <span className="email" title={ident.email}>{ident.name || ident.email}</span>
              {canMark && (
                <button className="token-btn" type="button" onClick={() => setTokenOpen(true)} title="Personal token for agents / CLI">
                  token
                </button>
              )}
              <button className="logout" type="button" onClick={signOut}>log out</button>
            </div>
          )}
        </div>
        {meta && (
          <p className="sub">
            scan{' '}
            {scans.length > 1 && asof ? (
              <>
                <select className="scanpick" value={asof} onChange={e => setDP(e.target.value)} aria-label="Scan date">
                  {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
                </select>
                {/* A sub-daily id already carries its time; a date-only one
                    (the daily GCS job) doesn't, so show when it was published. */}
                {asof && !/[T ]\d{2}/.test(asof) && meta.published && (
                  <Tooltip content={<>snapshot published {new Date(meta.published).toISOString().replace('T', ' ').slice(0, 16)} UTC</>}>
                    <span className="pub dotted">
                      {new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }).format(new Date(meta.published))}
                    </span>
                  </Tooltip>
                )}
              </>
            ) : (
              <b>{meta.asof}</b>
            )}
            {' '}·{' '}
            <Tooltip content={<>
              {units === 'si' ? 'SI units (TB) — click for IEC (TiB)' : 'IEC units (TiB) — click for SI (TB)'}
              {'; shift-click to '}{suffixB ? 'drop' : 'restore'} the “B”
            </>}>
              <b className="units-toggle" onClick={e => (e.shiftKey ? toggleSuffixB : toggleUnits)()}>{fmtBytes(meta.total_bytes)}</b>
            </Tooltip>
            {' '}· <b>{fmtN(meta.total_objects)}</b> objects
            {estCost && (
              <>
                {' '}· est. <b>${Math.round(estCost.list).toLocaleString()}/mo</b>{' '}
                <Tooltip content={<ClassMixTip mix={meta.class_bytes} note="GCS list prices (US regions) × scanned bytes; actual spend depends on the billing account's negotiated rates/credits" />}>
                  <span className="dotted">at list price</span>
                </Tooltip>
              </>
            )}
          </p>
        )}
        {/* Ambiguous `?d`: render the newest match (a best guess beats a dead
            end) with a strip listing every candidate to pin one. */}
        {dMatches.length > 1 && (
          <p className="disambig">
            <code>?d={encodeScan(dP) ?? dP}</code> matches {dMatches.length} scans — showing the newest; pin one:
            {dMatches.map(s => (
              <button key={s} className={s === asof ? 'on' : ''} onClick={() => setDP(s)}>{fmtScan(s)}</button>
            ))}
          </p>
        )}
      </header>

      {markMode && (
        <section className="mark-banner">
          <p>
            <b>Mark &amp; sweep</b> — unmarked data is <b>deleted permanently</b> after{' '}
            <b>EOD Friday Aug&nbsp;28</b> (<b>{sweepDaysLeft()} day{sweepDaysLeft() === 1 ? '' : 's'} left</b>).
            Drill to a prefix and mark it with the controls above the map, or click a cell to pin it and mark from
            there. <span className="mk-key"><span className="sw" style={{ background: 'var(--mk-keep)' }} />keep</span>{' '}
            <span className="mk-key"><span className="sw" style={{ background: 'var(--mk-klc)' }} />keep last checkpoint</span>{' '}
            <span className="mk-key"><span className="sw" style={{ background: 'var(--mk-del)' }} />delete</span>{' '}
            — the most recent mark covering a prefix wins: mark a child <em>after</em> its parent to
            carve an exception; a broad mark repaints older deeper ones (you'll be asked to confirm).
            {markIdx.count > 0 && <> <b>{markIdx.count}</b> mark{markIdx.count === 1 ? '' : 's'} so far.</>}
            {marksQ.error && <span className="err"> marks unavailable: {marksQ.error.message}</span>}
          </p>
        </section>
      )}

      <section className="prose">
        {store.key === 'cw' ? (
          <p>
            Storage in the <code>marin-us-east-02a</code> CoreWeave S3 bucket, from a per-object
            listing of the whole bucket. Treemap drills into prefixes; cells are colored by prefix,
            splitting any prefix that owns most of the bucket one level deeper (so <code>marin/</code>’s
            children get their own hues instead of one blue mass). The age chart still groups by
            top-level prefix.
          </p>
        ) : (
        <p>
          Storage across the six <code>marin-*</code> GCS buckets, from the weekly{' '}
          <a href="https://github.com/marin-community/marin/blob/main/scripts/ops/storage/" target="_blank" rel="noreferrer">Ops&nbsp;-&nbsp;Storage&nbsp;Report</a>{' '}
          scan (per-object listing, deduped). Treemap drills into prefixes; the “color by” control recolors
          both plots — by owning group (OA / Stanford / communal), top-level tree, age (older→newer), or owning
          user (hi-contrast, or hues grouped by group). Ownership comes from the{' '}
          <code>marin-gcs-usage</code> attribution pipeline (W&B run/config joins, executor sidecars, manual
          curation) — hover a cell for its group split and top users, or <kbd>⌘K</kbd> to jump to a user/group.
        </p>
        )}
      </section>

      {hasAttr && (
        <div className="colorctl" role="radiogroup" aria-label="Color plots by">
          <span className="lbl">color by</span>
          {MODES.filter(m => m !== 'read' || readRange).map(m => {
            const btn = (
              <button
                key={m}
                role="radio"
                aria-checked={effMode === m}
                className={effMode === m ? 'on' : ''}
                onClick={() => setMode(m)}
              >
                {MODE_LABELS[m]}
              </button>
            )
            return m === 'date' ? (
              <Tooltip key={m} content={<>
                colors by object <b>creation time</b>, from the bucket listings (each cell = the
                byte-weighted mean of its objects). GCS objects are immutable, so created ≈ last-modified.
                For access time, see the <b>read</b> lens.
              </>}>
                {btn}
              </Tooltip>
            ) : m === 'read' ? (
              <Tooltip key={m} content={<>
                colors by <b>last read</b> — the most recent GET/HEAD/LIST anywhere under each cell,
                from the GCS usage logs (logging began {readRange ? epochDaysToDate(readRange.min) : '—'}).
                Brick-red = <b>never read</b> since then: prime sweep candidates.
              </>}>
                {btn}
              </Tooltip>
            ) : btn
          })}
          <span className="filterbox">
            <input
              value={fq ?? ''}
              onChange={e => setFq(e.target.value || undefined)}
              placeholder="filter names — text or /regex/"
              aria-label="Filter tree by segment name"
              size={22}
            />
            {pred && shownTree && tree && (
              <span className="fnote">
                {shownTree.b > 0
                  ? <>{fmtBytes(shownTree.b)} matched ({((100 * shownTree.b) / tree.b).toFixed(1)}%)</>
                  : 'no matches'}
                <button type="button" title="clear filter" onClick={() => setFq(undefined)}>✕</button>
              </span>
            )}
          </span>
          {hl && (
            <button className="hlchip" onClick={clearHl} title="Clear highlight (x)">
              {hlUser ?? hlTeam} ✕
            </button>
          )}
        </div>
      )}

      {markMode && shownTree && (
        <MarkTabs root={shownTree} idx={markIdx} myUser={myUser}
          viewUser={viewUser} setViewUser={setMuP}
          users={mkUsers}
          tab={markTab} setTab={setMarkTab}
          scoped={scoped} setScoped={setScoped}
          openPath={openPath}
          hasAtime={!!readRange}
        />
      )}

      {mapTree ? (
        <>
          {/* Remount per store: the treemap's caches are tied to the tree it
              mounted with, and a switch can swap `tree` without ever passing
              through null once both payloads are cached. */}
          <Treemap
            key={store.key}
            root={mapTree}
            mode={effMode}
            userIdx={userIdx}
            dateRange={dateRange}
            readRange={readRange}
            hl={effHl}
            pricing={pricing}
            lens={lens}
            scheme={store.scheme}
            markIdx={markMode ? markIdx : undefined}
            path={mapPath}
            onPathChange={onMapPath}
          />
          {mapPath && (
            <ChildrenTable
              node={mapPath[mapPath.length - 1]}
              segs={mapPath.slice(1).map(n => n.n)}
              scheme={store.scheme}
              markIdx={markMode ? markIdx : undefined}
              onOpen={openPath}
            />
          )}
        </>
      ) : (
        <p className="loading">loading tree…</p>
      )}

      {diff && diff.rows.length > 0 && (
        <section>
          <h2>Changes since previous scan</h2>
          <p className="sub">
            {diff.prev ? fmtScan(diff.prev) : 'previous'} → {diff.curr ? fmtScan(diff.curr) : 'this scan'}
            {' '}· <b className={diff.total_b >= diff.total_a ? 'grew' : 'shrank'}>
              {(diff.total_b >= diff.total_a ? '+' : '−') + fmtBytes(Math.abs(diff.total_b - diff.total_a))}
            </b>
            {' '}· Δobjects {(diff.objects_b - diff.objects_a).toLocaleString('en-US')}
            {diff.truncated && ' · (largest changes shown; walk was budget-capped)'}
          </p>
          <DiffTreemap data={diff} label={store.title} />
        </section>
      )}

      <section>
        {/* Granularity is auto-picked (and user-switchable) inside AgeChart, so
            the heading stays unit-free rather than lying about "month". */}
        <h2>Bytes by created date</h2>
        <p className="sub">
          When today’s objects were written (created-time strata, colored by {MODE_LABELS[effMode]}).
        </p>
        {/* age.json strata carry no read info — the read lens falls back to age here */}
        {age.length > 0 && <AgeChart rows={age} catOrder={catOrder} mode={effMode === 'read' ? 'date' : effMode} userIdx={userIdx} />}
      </section>

      {rules && tree?.tm && meta?.users && (
        <AttributionRules rules={rules} tree={tree} users={meta.users} />
      )}

      {meta && store.prices && (
        <section>
          <h2>Storage classes</h2>
          <table className="classes">
            <thead>
              <tr><th>class</th><th>bytes</th><th>est. $/mo (list, US)</th></tr>
            </thead>
            <tbody>
              {Object.entries(meta.class_bytes)
                .sort((a, b) => b[1] - a[1])
                .map(([c, b]) => (
                  <tr key={c}>
                    <td>
                      <Tooltip placement="right" content={<>${CLASS_PRICE_US[c] ?? 0.02}/GiB·mo · {((100 * b) / meta.total_bytes).toFixed(1)}% of scanned bytes</>}>
                        <span className="dotted">{CLASS_NAMES[c] ?? c}</span>
                      </Tooltip>
                    </td>
                    <td>{fmtBytes(b)}</td>
                    <td>${Math.round((b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02)).toLocaleString()}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </section>
      )}

      <SpeedDial actions={[
        { key: 'github', label: 'GitHub', icon: <FaGithub />, href: REPO_URL },
        { key: 'lens', label: `Class lens: ${lens ? 'on' : 'off'} (s)`, icon: <MdLayers />, onClick: () => setLens(v => !v) },
        {
          key: 'theme',
          label: `Theme: ${theme}`,
          icon: theme === 'light' ? <MdLightMode /> : theme === 'dark' ? <MdDarkMode /> : <MdBrightnessAuto />,
          onClick: cycleTheme,
        },
      ]} />
      <Omnibar placeholder="Users, groups, color modes, scans…" maxResults={15} />
      <ShortcutsModal />
    </main>
  )
}

export default function App() {
  return (
    <HotkeysProvider config={{ storageKey: 'gcs-usage' }}>
      <AppContent />
    </HotkeysProvider>
  )
}
