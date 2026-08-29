import { useQueries, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdLayers, MdLightMode } from 'react-icons/md'
import { HotkeysProvider, Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AGE_MODES, AgeChart } from './AgeChart'
import { Avatar } from './Avatar'
import { canonId, ghHandle, shortName, shortUserKey } from './UserChip'
import { signInUrl, useCanMark, useIdent as useIdentity } from './auth'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap } from './DiffTreemap'
import type { DiffData } from './DiffTreemap'
import { buildUserIndex, epochDaysToDate } from './colors'
import { ChildrenTable } from './ChildrenTable'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
import { applyFilter, applyNodeFilter, collectMatches, parseQuery } from './filterTree'
import { BulkBar } from './BulkBar'
import { setCurrentScan, useMarkIndex, useMarks } from './marks'
import { LensBar, SCOPABLE } from './LensBar'
import type { Lens } from './LensBar'
import { applyTodoFilter, klcSplits, lensNodePred, teamLens, useMyUser, userLens } from './sweep'
import { MarkHistory } from './MarkHistory'
import { SiteNav } from './SiteNav'
import { SizeOverTime } from './SizeOverTime'
import { STORES, storeForPath } from './stores'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_NAMES, CLASS_PRICE_US, MODE_LABELS, classMix, fmtN, ratePerByte } from './types'
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
  // Which object store to render comes from the path (one store today; the
  // abstraction stays so a second cloud store is a `STORES` row + data).
  const { pathname, search, hash } = useLocation()
  const navigate = useNavigate()
  const store = storeForPath(pathname)
  const canMark = useCanMark()
  // Mark & sweep (specs/mark-sweep-ui.md): the same treemap plus keep/sweep
  // controls, shown to any signed-in marker on the GCS store — anon and guest
  // (no-email) sessions get the read-only view. Folded onto `/` (was a separate
  // `/mark` route); GCS only, since CoreWeave is out of the sweep.
  const markMode = store.key === 'gcs' && canMark
  const marksQ = useMarks(markMode)
  const markIdx = useMarkIndex(marksQ.data)
  // Keep the tab title in sync with the store on client-side navigation.
  useEffect(() => {
    document.title = store.title
  }, [store])
  // URL token matches the visible label ("written"/"group"/"mark"), not the
  // internal key ("date"/"team"/"fate"); old ?c=age / ?c=fate links still decode.
  // ABSENT is meaningful: it means "the lens-appropriate default" (see `mode`
  // below), so switching lenses re-defaults the coloring — but an explicit
  // pick (any `?c=`, including group) survives every lens change.
  const modeCodec = {
    encode: (v: string | undefined) => (v === undefined ? undefined : v === 'date' ? 'written' : v === 'team' ? 'group' : v === 'fate' ? 'mark' : v),
    decode: (e: string | undefined) => (e === undefined ? undefined : e === 'written' || e === 'age' ? 'date' : e === 'group' ? 'team' : e === 'mark' || e === 'fate' ? 'fate' : e),
  }
  const [modeP, setModeP] = useUrlState('c', modeCodec)
  // The age chart's own color axis (`?ac=`, same tokens); absent = follow the map.
  const [ageModeP, setAgeModeP] = useUrlState('ac', modeCodec)
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
    // Throw on a non-OK response (e.g. a 401 from the data proxy with no
    // session) so react-query holds it as an error instead of handing the
    // error body downstream — `scans` then stays `[]` rather than crashing
    // `scans.map`, and the error surfaces as a sign-in prompt below.
    queryFn: async () => {
      const r = await fetch(`${store.base}/scans.json`)
      if (!r.ok) throw Object.assign(new Error(`scans: ${r.status}`), { status: r.status })
      return r.json()
    },
    refetchInterval: SCANS_POLL_MS,
  })
  const rulesQ = useQuery({
    queryKey: ['rules'],
    queryFn: async () => {
      const r = await fetch('/data/rules.json')
      if (!r.ok) throw new Error(`rules: ${r.status}`)
      return r.json()
    },
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
  // `?f=` (name filter) and `?l=` (review lens) read early: they decide
  // whether the full artifact tree is needed at all (see treeQ below).
  // `?l=` replaces legacy `?mt=` (value `user` was `mine` — the lens views
  // *a* user, not necessarily you); `?u=` replaces `?mu=`, encoded as the
  // user's shortest registry alias. Old links normalize below.
  const [fq, setFq] = useUrlState('f', stringParam())
  const [markTabP, setMarkTabP] = useUrlState('l', stringParam())
  // tree.json is ~29MB — the estate-wide walks (name filter's match set +
  // re-aggregation, lens scoping) still need its depth, but plain browsing
  // doesn't: the map seeds from the same pixel-budget /api/subtree that
  // serves drills. So the full tree only downloads when a filter or lens is
  // active (or for stores with no path index, where it's the only source).
  // Mark mode needs it too: the keep/sweep/undecided rollup resolves marks
  // against whatever tree is loaded, and the pixel-budget subtree folds most
  // mark prefixes away — so the coarse tree understated sweep by ~100 Ti and
  // the numbers jumped the moment a lens pulled tree.json in. Same tree every
  // time, or the totals aren't comparable (specs/exact-fate-totals.md for the
  // floor-free version).
  const needFullTree = store.key !== 'gcs' || fq != null || markTabP != null || markMode
  // One-time legacy-param rewrite (`mt`/`mu` → `l`/`u`), so old links work
  // and re-share in the golfed form.
  useEffect(() => {
    const sp = new URLSearchParams(search)
    if (!sp.has('mt') && !sp.has('mu')) return
    const mt = sp.get('mt')
    const mu = sp.get('mu')
    sp.delete('mt')
    sp.delete('mu')
    if (mt) sp.set('l', mt === 'mine' ? 'user' : mt)
    if (mu) sp.set('lu', shortUserKey(canonId(mu)))
    navigate({ pathname, search: `?${sp.toString()}`, hash }, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])
  const treeQ = useQuery({ ...scanQuery<TreeNode>('tree'), enabled: !!asof && needFullTree })
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
  // Lazy drill (specs/path-index-lazy-drill.md step 3, now the primary
  // source): the map's base is the pixel-budget subtree at the store root,
  // and every level of the drilled path gets its own subtree query, grafted
  // in depth order — interactive drills hit each level's cache as they go,
  // and a cold deep link fans the whole chain out in parallel. tree.json is
  // only the base when it's already needed (filter/lens) or the store has no
  // path index (CW).
  const graftPath = pathname.slice((store.path === '/' ? '' : store.path).length).replace(/^\/+/, '')
  const canW = Math.ceil((typeof window === 'undefined' ? 1280 : window.innerWidth) / 128) * 128
  const subtreePaths = useMemo(() => {
    if (store.key !== 'gcs') return []
    const segs = graftPath.split('/').filter(Boolean)
    return ['', ...segs.map((_, i) => segs.slice(0, i + 1).join('/'))]
  }, [store.key, graftPath])
  const subtreeQs = useQueries({
    queries: subtreePaths.map(p => ({
      queryKey: ['subtree', store.key, asof, p, canW],
      enabled: !!asof,
      staleTime: Infinity,
      retry: false,
      queryFn: async () => {
        const r = await fetch(
          `/api/subtree?date=${asof}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}`,
          { credentials: 'include' },
        )
        return r.ok ? (r.json() as Promise<{ tree: TreeNode }>) : null
      },
    })),
  })
  const rootSub = subtreeQs[0]?.data?.tree ?? null
  const baseTree: TreeNode | null = treeQ.data ?? (store.key === 'gcs' ? rootSub : null)
  // useQueries returns a fresh array each render; stamp the data so the graft
  // memo re-runs exactly when a response lands.
  const subStamp = subtreeQs.map(q => q.dataUpdatedAt).join(',')
  const tree = useMemo((): TreeNode | null => {
    if (!baseTree) return null
    const graftAt = (t: TreeNode, segs: string[], sub: TreeNode): TreeNode => {
      const rec = (n: TreeNode, i: number): TreeNode => {
        if (i === segs.length) return { ...n, c: sub.c } // keep own totals; adopt finer children
        const seg = segs[i]
        const kids = n.c ?? []
        if (kids.some(k => k.n === seg)) return { ...n, c: kids.map(k => (k.n === seg ? rec(k, i + 1) : k)) }
        // The spine segment fell below this level's pixel budget (it's inside
        // "(other)"): synthesize it from its own subtree response — the
        // response root carries the real totals — and shave those bytes off
        // the fold so the level still sums. Deeper segments wait for their
        // own level's graft to land.
        if (i !== segs.length - 1) return n
        const c = kids.map(k =>
          k.n === '(other)' ? { ...k, b: Math.max(0, k.b - sub.b), o: Math.max(0, k.o - sub.o) } : k)
        return { ...n, c: [...c, { ...sub, n: seg }] }
      }
      return rec(t, 0)
    }
    let t = baseTree
    subtreePaths.forEach((p, i) => {
      const sub = subtreeQs[i]?.data?.tree
      if (p && sub) t = graftAt(t, p.split('/'), sub)
    })
    return t
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseTree, subtreePaths, subStamp])

  // keep_last_ckpt → concrete keep/sweep split, resolved against the loaded
  // tree (fate cells, stripes, and the fate rollup all decompose through it).
  const klcIdx = useMemo(
    () => (tree && markIdx.count ? klcSplits(tree, markIdx.keeps) : undefined),
    [tree, markIdx],
  )
  const pred = useMemo(() => (fq ? parseQuery(fq) : null), [fq])
  const shownTree = useMemo(() => (tree && pred ? applyFilter(tree, pred) : tree), [tree, pred])
  const fMatches = useMemo(() => (tree && pred ? collectMatches(tree, pred) : []), [tree, pred])
  const age: AgeRow[] = ageQ.data ?? []
  const meta: Meta | null = metaQ.data ?? null
  // Deep-link to a section via `#hash` (e.g. `…/ego-dex#size-over-time`). Re-runs
  // as each data source lands (sections mount off different queries), and defers
  // to the next frame so the target exists and is laid out before we scroll.
  useEffect(() => {
    if (!hash) return
    const id = hash.slice(1)
    // The treemap/table lay out async and shift the page after first paint, so a
    // single deferred scroll lands in the wrong place (or a still-empty page).
    // Re-scroll over ~2s until the anchor's position stops moving.
    let last = NaN
    const timers = [150, 400, 800, 1400, 2000].map(ms => setTimeout(() => {
      const el = document.getElementById(id)
      if (!el) return
      const top = el.getBoundingClientRect().top
      if (Math.abs(top) < 4 && top === last) return // already parked at the top
      last = top
      el.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, ms))
    return () => timers.forEach(clearTimeout)
  }, [hash, tree, meta, scans])
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const { units, suffixB, fmtBytes, toggleUnits, toggleSuffixB } = useUnits()
  const [theme, cycleTheme] = useTheme()
  const ident = useIdentity()
  const myUser = useMyUser(ident?.email, markMode)
  // `?mt=` (declared above, near treeQ) — active review lens over the map +
  // children table (absent = no lens, the plain browse view). The lenses are
  // presets on the normal view (LensBar), scoped to the current subtree —
  // there's no separate /mark page.
  // No email (anon / no-email session) → no "My files" lens to attribute to.
  const hasEmail = !!ident?.email
  const markTabRaw: Lens =
    markTabP === 'user' || markTabP === 'mine' ? 'mine'
    : markTabP === 'todo' || markTabP === 'unclaimed' || markTabP === 'communal' ? markTabP
    : 'all'
  const markTab: Lens = markTabRaw === 'mine' && !hasEmail ? 'all' : markTabRaw
  const setMarkTab = (t: Lens) => {
    setMarkTabP(t === 'all' ? undefined : t === 'mine' ? 'user' : t)
    if (t !== 'mine') setUP(undefined) // `lu` is meaningless outside the user lens
  }
  // `?lu=` — whose files the user lens shows (anyone's view is browsable;
  // `?u=` is the highlight-user param). Only meaningful while that lens is
  // active: an inactive lens must not redecorate its chip or the view.
  const [uP, setUP] = useUrlState('lu', stringParam())
  const viewUser = markTab === 'mine' ? ((uP ? canonId(uP) : null) ?? myUser) : null
  // `?ms=0` — scope-map-to-view toggled off (on is the default).
  const [scopedP, setScopedP] = useUrlState('ms', stringParam())
  const scoped = scopedP !== '0'
  const setScoped = (v: boolean) => setScopedP(v ? undefined : '0')
  // The treemap's drill path now lives in the URL *path* (below the store's own
  // route prefix), so a drilled prefix is a real shareable URL —
  // `/marin-us-central1/ego-dex`, not `/?p=marin-us-central1/ego-dex`. View
  // options stay query params (`?c`, `?mt`, …); the section stays in the `#hash`.
  const storeBase = store.path === '/' ? '' : store.path
  const drillPath = pathname.slice(storeBase.length).replace(/^\/+/, '')
  const drillTo = (segs: string[]) =>
    navigate({ pathname: segs.length ? `${storeBase}/${segs.join('/')}` : store.path, search, hash })
  // Read-recency lens domain: the access-log observation window (meta), not
  // the tree's own min/max — "no reads" is only meaningful vs when logging began.
  const readRange = useMemo((): DateRange | null =>
    meta?.access ? { min: meta.access.from, max: meta.access.to } : null,
  [meta])
  // No explicit `?c=` → a lens-appropriate default; an explicit pick always
  // wins. During the cleanup sprint the primary axis is mark state ("marks"),
  // so the fill and the keep/sweep decorations are ONE axis — group shading
  // (with mark borders as a colliding second color axis) is opt-in, not the
  // landing view. Per-owner lenses default to `user` instead (fate is useless
  // on an all-undecided view; group is useless on a single-owner one).
  const lensDefaultMode: ColorMode =
    markTab === 'todo' || markTab === 'mine' ? 'user' : markMode ? 'fate' : 'team'
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : lensDefaultMode
  const setMode = (m: ColorMode) => setModeP(m)
  const hasAttr = !!tree?.tm
  const effMode: ColorMode =
    (mode === 'read' && !readRange) || (mode === 'fate' && !markMode) ? 'team' : hasAttr ? mode : 'tree'
  // The age chart's color axis: an explicit `?ac=` wins; otherwise it follows
  // the map, except marks (no per-stratum value in age.json) → written. The
  // read axis needs strata that carry `a` (scans published from 8/29 on) —
  // without them it's offered disabled and the chart falls back to written.
  const ageReadRange = age.some(r => r.a != null) ? readRange : null
  const ageMode: ColorMode = (() => {
    const want: ColorMode = ageModeP && (AGE_MODES as string[]).includes(ageModeP) ? (ageModeP as ColorMode) : effMode === 'fate' ? 'date' : effMode
    return (want === 'read' && !ageReadRange) || (!hasAttr && want !== 'date' && want !== 'tree') ? 'date' : want
  })()
  const hl: Highlight | null = hlUser ? { user: hlUser } : hlTeam ? { team: hlTeam } : null
  // In mark mode the active tab scopes the map to its lens (filter +
  // re-aggregate — the worklists keep the unscoped tree, so their
  // maximal-subtree rows don't coarsen); untoggled, fall back to dimming.
  // `todo` scopes by mark state instead of an attribution lens: prune every
  // subtree already covered by a keep/sweep decision, show what's undecided.
  const scopedActive = markMode && scoped && SCOPABLE.includes(markTab) && (markTab !== 'mine' || !!viewUser)
  const todoActive = markMode && scoped && markTab === 'todo'
  // Any review lens narrower than "all" — sections whose data can't follow
  // the lens (series/age charts) hide rather than show fleet-wide numbers.
  const lensScoped = markMode && markTab !== 'all'
  const mapTree = useMemo(() => {
    if (!shownTree) return shownTree
    if (todoActive) return applyTodoFilter(shownTree, markIdx)
    if (!scopedActive) return shownTree
    const lens = markTab === 'mine'
      ? userLens(viewUser!)
      : teamLens(markTab === 'unclaimed' ? 'unattributed' : 'communal')
    return applyNodeFilter(shownTree, lensNodePred(lens))
  }, [shownTree, scopedActive, todoActive, markIdx, markTab, viewUser])
  // Controlled treemap drill path, resolved against the (possibly filtered/
  // scoped) tree each render: `?p=` survives scope toggles, filters, and scan
  // switches by re-walking the new tree; a vanished path truncates to its
  // deepest surviving ancestor.
  const mapPath = useMemo((): TreeNode[] | undefined => {
    if (!mapTree) return undefined
    const path = [mapTree]
    let cur: TreeNode = mapTree
    for (const s of drillPath.split('/').filter(Boolean)) {
      const next = cur.c?.find(c => c.n === s)
      if (!next) break
      path.push(next)
      cur = next
    }
    // A store with one bucket (CoreWeave today) opens inside it — the bucket
    // level is a single full-width box otherwise.
    if (path.length === 1 && mapTree.c?.length === 1) return [mapTree, mapTree.c[0]]
    return path
  }, [mapTree, drillPath])
  const onMapPath = (p: TreeNode[]) => drillTo(p.slice(1).map(n => n.n))
  // Worklist rows / children table → drill the map to a prefix and show it.
  const openPath = (segs: string[]) => {
    drillTo(segs)
    document.querySelector('.dt-treemap')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
  // A scopable lens highlights its slice even when "scope map" is off; with no
  // lens (all / to-do) fall back to the manually-picked highlight.
  const effHl: Highlight | null =
    // The user lens highlights its user scoped or not: a scoped subtree still
    // contains minority co-tenants, and user coloring should dim them.
    markTab === 'mine' && viewUser ? { user: viewUser }
    : scopedActive ? null
    : markTab === 'unclaimed' ? { team: 'unattributed' }
    : markTab === 'communal' ? { team: 'communal' }
    : hl

  // The CoreWeave dashboard is a sibling deployment (`cw-s3` branch), not a
  // store of this app — cross-link it.
  const crossSite = { label: 'CoreWeave usage', href: 'https://cw-s3.oa.dev/' }

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
          <SiteNav inline />
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
            <b>Mark &amp; sweep</b> — review storage and mark what to <b>keep</b>; anything left
            unmarked is <b>swept</b> (deleted) once the review window closes (closing date TBD —
            announced in advance; only explicit <b>sweep</b> marks are deleted before then). Drill to a prefix and
            mark it with the controls above the map, or click a cell to pin it and mark from there.
            Marks are reversible until the sweep — the most recent mark covering a prefix wins: mark a
            child <em>after</em> its parent to carve an exception; a broad mark repaints older deeper
            ones (you'll be asked to confirm).
            {markIdx.count > 0 && <> <b>{markIdx.count}</b> mark{markIdx.count === 1 ? '' : 's'} so far.</>}
            {marksQ.error && <span className="err"> marks unavailable: {marksQ.error.message}</span>}
            {' '}Pick a <b>lens</b> below to focus a slice (your files, unclaimed, communal) or the
            to-do list, scoped to whatever you've drilled into.
          </p>
        </section>
      )}

      <section className="prose">
        <p>
          Storage across the six <code>marin-*</code> GCS buckets — a full per-object listing
          (deduped), snapshotted daily by the{' '}
          <a href="https://github.com/Open-Athena/marin-gcs-usage/blob/gcs/AGENTS.md#data-flow" target="_blank" rel="noreferrer"><code>marin-gcs-usage</code></a>{' '}
          pipeline, which also ingests the buckets’ access logs and joins ownership onto every prefix
          (W&B run/config matching, executor sidecars, manual curation). The treemap drills into
          prefixes; “color by” recolors both plots — <b>marks</b> (keep / sweep / undecided; the
          default), <b>read</b> (last-read recency — never-read bytes are the best sweep candidates,
          though access logging only began {readRange ? epochDaysToDate(readRange.min) : '8/13'}, so
          “never read” means “not since then”),
          owning user or group (OA / Stanford / communal), written (older→newer), or top-level tree.
          Marks and claims apply live on top of the latest snapshot. Hover a cell for its makeup and
          top users, <kbd>⌘K</kbd> to jump to a user/group, or see the per-user breakdown at{' '}
          <Link to="/users">/users</Link>.
        </p>
      </section>

      {scansQ.isError && (
        <p className="tab-note" style={{ color: 'var(--s3)' }}>
          Couldn’t load snapshot data ({(scansQ.error as { status?: number })?.status === 401 ? 'not signed in — this dashboard is access-gated' : String(scansQ.error)}).
          {' '}<a href={signInUrl()}>Sign in</a> or reload once your session is active.
        </p>
      )}

      {markMode && (
        <LensBar
          idx={markIdx}
          hasEmail={hasEmail}
          myUser={myUser}
          viewUser={viewUser} setViewUser={u => setUP(u ? shortUserKey(canonId(u)) : undefined)}
          users={mkUsers}
          lens={markTab} setLens={setMarkTab}
          scoped={scoped} setScoped={setScoped}
        />
      )}

      {hasAttr && (
        <div className="colorctl" role="radiogroup" aria-label="Color plots by">
          <span className="lbl">color by</span>
          {MODES.filter(m => (m !== 'read' || readRange) && (m !== 'fate' || markMode)).map(m => {
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
              placeholder="filter paths — text, a|b, or /regex/"
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
          {pred && fq && fMatches.length > 0 && (
            <BulkBar matches={fMatches} scheme={store.scheme} query={fq} />
          )}
          {hl && (
            <button className="hlchip" onClick={clearHl} title="Clear highlight (x)">
              {hlUser ?? hlTeam} ✕
            </button>
          )}
        </div>
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
            klcIdx={markMode ? klcIdx : undefined}
            fateReady={!!treeQ.data}
            path={mapPath}
            onPathChange={onMapPath}
          />
          {/* The map's own listing — this node's children, narrowed to the active
              lens (to-do drops already-decided prefixes). */}
          {mapPath && (
            <ChildrenTable
              node={mapPath[mapPath.length - 1]}
              segs={mapPath.slice(1).map(n => n.n)}
              scheme={store.scheme}
              markIdx={markMode ? markIdx : undefined}
              todoOnly={markMode && markTab === 'todo'}
              onOpen={openPath}
            />
          )}
        </>
      ) : (
        <p className="loading">loading tree…</p>
      )}

      {/* Under the To-do lens the series chart flips to mark-progress (the
          ledger replayed per scan — specs/lens-aware-time-series.md); other
          lenses still hide it (their data can't scope), as does the age chart
          below until /api/age lands. */}
      {(!lensScoped || markTab === 'todo') && (
        <SizeOverTime scans={scans} prefix={drillPath} base={store.base} fate={markTab === 'todo'} />
      )}

      {markMode && <MarkHistory prefix={store.scheme + drillPath} scope={drillPath || 'all buckets'} />}

      {diff && diff.rows.length > 0 && (
        <section id="changes">
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

      {!lensScoped && (
      <section id="created-date">
        {/* Granularity is auto-picked (and user-switchable) inside AgeChart, so
            the heading stays unit-free rather than lying about "month". */}
        <h2>Bytes by creation date</h2>
        <p className="sub">
          When each stored byte was <b>written</b> — the object’s creation time from the listing.
          GCS objects are immutable, so there’s no separate “modified” time; the other time axis is{' '}
          <b>last read</b> (from the usage logs, since {readRange ? epochDaysToDate(readRange.min) : '8/13'}) —
          color by it to see which vintages nobody has touched. The chart’s color axis is its own (right):
          it follows the map’s until you pick one; marks have no per-stratum value here.
        </p>
        {age.length > 0 && (
          <AgeChart rows={age} catOrder={catOrder} mode={ageMode} onMode={m => setAgeModeP(m)} userIdx={userIdx} readRange={ageReadRange} />
        )}
      </section>
      )}

      {meta && store.prices && (() => {
        // Class mix of the *drilled* node (each node carries descendant-inclusive
        // `cb`), so this tracks the treemap instead of always showing fleet totals.
        const node = mapPath ? mapPath[mapPath.length - 1] : tree
        if (!node) return null
        const mix = classMix(node)
        const total = node.b || 1
        const scope = mapPath && mapPath.length > 1 ? mapPath.slice(1).map(n => n.n).join('/') : 'all buckets'
        return (
          <section id="storage-classes">
            <h2>Storage classes</h2>
            <p className="sub">
              Class mix + list-price estimate for <code>{scope}</code> — updates as you drill the treemap.
            </p>
            <table className="classes">
              <thead>
                <tr><th>class</th><th>bytes</th><th>est. $/mo (list, US)</th></tr>
              </thead>
              <tbody>
                {Object.entries(mix)
                  .sort((a, b) => b[1] - a[1])
                  .map(([c, b]) => (
                    <tr key={c}>
                      <td>
                        <Tooltip placement="right" content={<>${CLASS_PRICE_US[c] ?? 0.02}/GiB·mo · {((100 * b) / total).toFixed(1)}% of these bytes</>}>
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
        )
      })()}

      {/* Static attribution reference — how ownership is inferred + the rule tables.
          Reference material, so it sits last rather than sandwiched mid-page. */}
      {tree?.tm && <AttributionRules tree={tree} />}

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
