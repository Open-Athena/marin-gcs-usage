import { useQueries, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { SyntheticEvent } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link, useLocation } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdInfoOutline, MdLayers, MdLightMode } from 'react-icons/md'
import { Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AGE_MODES, AgeChart } from './AgeChart'
import { AttributionRules } from './AttributionRules'
import { Busy, Skeleton } from './Busy'
import { DiffTreemap } from './DiffTreemap'
import { SizeOverTime } from './SizeOverTime'
import type { DiffData } from './DiffTreemap'
import { buildUserIndex } from './colors'
import { DAY, fmtScan, nearestScan, scanTime, useScan } from './scan'
import { storeForPath } from './stores'
import { ClassMixTip, SpeedDialTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
import { ChildrenTable } from './ChildrenTable'
import { useHashSpy } from './hashSpy'
import { useMarks } from './marks'
import { DiffTable } from './DiffTable'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_NAMES, CLASS_PRICE_US, MODE_LABELS, fmtN, ratePerByte } from './types'
import { UserChip, shortName } from './UserChip'
import { useUnits } from './units'
const MODES = Object.keys(MODE_LABELS) as ColorMode[]
const REPO_URL = 'https://github.com/Open-Athena/marin-gcs-usage'

// CF Access identity (present when served behind gcs.oa.dev; absent in local dev)
interface Identity { email: string; name?: string }

function useIdentity(): Identity | null {
  const [ident, setIdent] = useState<Identity | null>(null)
  useEffect(() => {
    void fetch('/cdn-cgi/access/get-identity')
      .then(r => (r.ok ? r.json() : null))
      .then(d => d?.email && setIdent(d))
      .catch(() => {})
  }, [])
  return ident
}

// Edu-fold state: open for the viewer's FIRST session (the copy is onboarding
// — it earns its space once), collapsed by default ever after. An explicit
// toggle wins forever (localStorage); sessionStorage marks the grace session
// so a mid-session reload doesn't slam the fold shut on a first-time reader.
function useFold(key: string): [boolean, (e: SyntheticEvent<HTMLDetailsElement>) => void] {
  const [open, setOpen] = useState(() => {
    try {
      const chosen = localStorage.getItem(key)
      if (chosen != null) return chosen !== '0'
      if (localStorage.getItem(`${key}:seen`) == null) {
        localStorage.setItem(`${key}:seen`, '1')
        sessionStorage.setItem(`${key}:grace`, '1')
        return true
      }
      return sessionStorage.getItem(`${key}:grace`) != null
    } catch { return true }
  })
  const onToggle = (e: SyntheticEvent<HTMLDetailsElement>) => {
    const o = e.currentTarget.open
    if (o === open) return // browsers fire `toggle` when the attr is first set — not a choice
    setOpen(o)
    try { localStorage.setItem(key, o ? '1' : '0') } catch { /* in-memory only */ }
  }
  return [open, onToggle]
}

// Diff-section span presets (days back from the "after" scan).
const SPANS: [string, number][] = [['1d', 1], ['3d', 3], ['7d', 7], ['14d', 14], ['30d', 30]]

// Home-page section anchors, top to bottom — the scroll-spy keeps `#hash`
// tracking the one in view, and deep links scroll to it. Old ids keep working.
const SECTION_IDS = ['tree-map', 'over-time', 'diff', 'mtime']
const LEGACY_ANCHORS: Record<string, string> = { 'size-over-time': 'over-time', changes: 'diff', 'created-date': 'mtime' }
// Color-mode URL tokens: one letter each (`?c=a`, `?ac=t`); the older
// spelled-out forms still decode so shared links keep working.
const MODE_TOKENS: Record<string, ColorMode> = { a: 'date', age: 'date', t: 'tree', tree: 'tree', u: 'user', user: 'user' }
const modeToken = (m: ColorMode): string => (m === 'date' ? 'a' : m === 'tree' ? 't' : 'u')

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
  const { pathname, hash } = useLocation()
  const store = storeForPath(pathname)
  // Scan selection (`?d=YYMMDD`) + the polling scan list; absent `?d` is a
  // first-class "latest", so a parked tab follows new scans.
  const { asof, scans, setDP, span, setSpan, setRange, scansQ } = useScan(store)
  const marks = useMarks()
  const scanQuery = <T,>(name: string) => ({
    queryKey: [name, store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
  const ageQ = useQuery(scanQuery<AgeRow[]>('age'))
  const metaQ = useQuery(scanQuery<Meta>('meta'))
  const rulesQ = useQuery<Rules | null>({ queryKey: ['rules'], queryFn: () => fetch('/data/rules.json').then(r => (r.ok ? r.json() : null)).catch(() => null), staleTime: Infinity })
  const age: AgeRow[] = ageQ.data ?? []
  const meta: Meta | null = metaQ.data ?? null
  const rules: Rules | null = rulesQ.data ?? null
  // Controlled treemap drill path in `?path=` (bucket-prefixed segments,
  // `marin-us-east-02a/marin/...` — the index's own paths). It survives scan
  // switches by re-walking the new tree; a vanished path truncates to its
  // deepest surviving ancestor.
  const [drillPath, setDrillPath] = useUrlState('path', stringParam())
  const graftPath = (drillPath ?? '').replace(/^\/+|\/+$/g, '')
  // Every read is a view query (specs/view-serving.md): the map's base is the
  // pixel-budget subtree at the root, and every level of the drilled path
  // gets its own subtree query, grafted in depth order — interactive drills
  // hit each level's cache as they go, and a cold deep link fans the whole
  // chain out in parallel. w/h are the canvas budget (quantized to 128 px so
  // resizes mostly re-hit the edge cache).
  const canW = Math.ceil((typeof window === 'undefined' ? 1280 : window.innerWidth) / 128) * 128
  const subtreePaths = useMemo(() => {
    const segs = graftPath.split('/').filter(Boolean)
    return ['', ...segs.map((_, i) => segs.slice(0, i + 1).join('/'))]
  }, [graftPath])
  const subtreeQs = useQueries({
    queries: subtreePaths.map(p => ({
      queryKey: ['subtree', asof, p, canW],
      enabled: !!asof,
      staleTime: Infinity,
      // Retry transient failures, but not the deterministic ones (409: no
      // index for this scan; 413: view too wide) — those surface as-is.
      retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
      retryDelay: (n: number) => 400 * 2 ** n,
      queryFn: async () => {
        const r = await fetch(
          `/api/subtree?date=${asof}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}`,
          { credentials: 'include' },
        )
        if (!r.ok) throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`)
        return r.json() as Promise<{ tree: TreeNode; threshold?: number }>
      },
    })),
  })
  // Progressive fill: a companion `depth=1` fetch for the deepest path — the
  // same pixel budget, capped one level below the root, so it returns the
  // *identical* top-level children (branches arrive without `c`, already
  // drillable) from a single depth-band read. It stands in until the full
  // tree lands; because the top level matches, the fill-in adds children
  // under tiles that don't move.
  const coarseQs = useQueries({
    queries: subtreePaths.map((p, i) => ({
      queryKey: ['subtree', asof, p, canW, 'depth1'],
      enabled: !!asof && i === subtreePaths.length - 1,
      staleTime: Infinity,
      retry: false,
      queryFn: async () => {
        const r = await fetch(
          `/api/subtree?date=${asof}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}&depth=1`,
          { credentials: 'include' },
        )
        if (!r.ok) throw new Error(`${r.status}`)
        return r.json() as Promise<{ tree: TreeNode }>
      },
    })),
  })
  // The full tree for path i once it's here; for the DEEPEST path only, the
  // depth-1 one while it isn't (an ancestor must be full or absent: `mapPath`
  // walks the spine and truncates at the first node without children).
  const dataFor = (i: number): TreeNode | null =>
    subtreeQs[i]?.data?.tree ?? (i === subtreePaths.length - 1 ? coarseQs[i]?.data?.tree ?? null : null)
  const baseTree: TreeNode | null = dataFor(0)
  const rootErr = subtreeQs[0]?.error as Error | undefined
  // useQueries returns a fresh array each render; stamp the data so the graft
  // memo re-runs exactly when a response (either tier) lands.
  const subStamp = [...subtreeQs, ...coarseQs].map(q => q.dataUpdatedAt).join(',')
  const tree = useMemo((): TreeNode | null => {
    if (!baseTree) return null
    const graftAt = (t: TreeNode, segs: string[], sub: TreeNode): TreeNode => {
      const rec = (n: TreeNode, i: number): TreeNode => {
        // Keep own totals; adopt the finer children.
        if (i === segs.length) return { ...n, c: sub.c }
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
      const sub = dataFor(i)
      if (p && sub) t = graftAt(t, p.split('/'), sub)
    })
    return t
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseTree, subtreePaths, subStamp])
  // Hold the last tree that rendered while the next one loads — whole, so it
  // is self-consistent. The map never flashes to nothing across a scan or
  // drill change; at worst `mapPath` truncates the new drill to an ancestor
  // this tree still has, until the new tree (depth-1 first, then full)
  // replaces it. The map's derivations follow `shownTree`, not `tree`.
  const lastTree = useRef<TreeNode | null>(null)
  if (tree) lastTree.current = tree
  const shownTree = tree ?? lastTree.current
  const treeLoading = !tree && !!lastTree.current
  const mapBusy = treeLoading || subtreeQs.some(q => q.isFetching)
  // The Diff section's "before" endpoint comes from the `?d=` span (see
  // scan.ts): absent = the previous scan; a span resolves to the scan
  // *nearest* that far before "after" — scan times drift minutes past exact
  // multiples (0:01, 12:01…), so "at least N days back" would skip half a
  // cadence. The "after" endpoint IS the page's scan. Both sides are read
  // server-side (`/api/diff`) at the drilled path, at one shared byte floor.
  const prevScan = asof ? scans[scans.indexOf(asof) + 1] ?? null : null
  const earlier = useMemo(() => (asof ? scans.filter(s => s < asof) : []), [asof, scans])
  const spanScan = span && asof ? nearestScan(earlier, scanTime(asof) - span) : null
  const diffPrev = spanScan ?? prevScan
  // Hour-rounded span back from `to` — the previous scan clears it, anything
  // else round-trips as its own span (nearest-scan resolution recovers it,
  // and the link keeps following `latest`).
  const spanTo = (to: string, from: string): number | undefined =>
    scans[scans.indexOf(to) + 1] === from
      ? undefined
      : Math.max(3600_000, Math.round((scanTime(to) - scanTime(from)) / 3600_000) * 3600_000)
  const pickBefore = (scan: string) => { if (asof) setSpan(spanTo(asof, scan)) }
  // A brush on the size chart picks both endpoints at once: "after" becomes
  // the page's scan and "before" round-trips as its hour-rounded span. A
  // zero-width brush is a click (TimeSeries hands those to `onPickDate`).
  const brushRange = (from: string, to: string) => {
    if (to <= from) return
    setRange(to, spanTo(to, from))
  }
  const diffWindow: [string, string] | undefined = diffPrev && asof ? [diffPrev, asof] : undefined
  // Presets past the history's reach — nearest scan more than a quarter of
  // the span off, or already claimed by a shorter preset — are dropped
  // rather than mislabeled.
  const spanPicks = useMemo(() => {
    if (!asof) return []
    const t0 = scanTime(asof)
    const picks: { label: string; ms: number; scan: string }[] = []
    for (const [label, days] of SPANS) {
      const ms = days * DAY
      const best = nearestScan(earlier, t0 - ms)
      if (!best || Math.abs(scanTime(best) - (t0 - ms)) > ms / 4) continue
      if (!picks.some(p => p.scan === best)) picks.push({ label, ms, scan: best })
    }
    return picks
  }, [asof, earlier])
  const diffFetch = (extra: string) => async () => {
    const r = await fetch(
      `/api/diff?from=${diffPrev}&to=${asof}&path=${encodeURIComponent(graftPath)}&w=${canW}&h=${Math.round(canW * 0.6)}${extra}`,
      { credentials: 'include' },
    )
    if (!r.ok) throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`)
    return r.json() as Promise<DiffData>
  }
  // First paint: the top-level diff (`depth=1` — the two root reads plus one
  // level of lookups) stands in for the full walk while it aligns, so the
  // map shows the shape of the change before its detail.
  const diffQ1 = useQuery<DiffData, Error>({
    queryKey: ['diff', diffPrev, asof, graftPath, canW, 'l1'],
    enabled: !!asof && !!diffPrev,
    staleTime: Infinity,
    retry: false,
    queryFn: diffFetch('&depth=1'),
  })
  const diffL1 = diffQ1.data
  const diffQ = useQuery<DiffData, Error>({
    queryKey: ['diff', diffPrev, asof, graftPath, canW],
    enabled: !!asof && !!diffPrev,
    // While the full walk aligns: the top-level diff of the SAME pair once it
    // lands, else the last pair's diff — drawn dimmed either way, so the
    // section holds its height and shows something before the detail.
    placeholderData: (prev: DiffData | undefined) => diffL1 ?? prev,
    staleTime: Infinity,
    retry: (n: number, e: Error) => !/^4\d\d/.test(e.message) && n < 3,
    retryDelay: (n: number) => 400 * 2 ** n,
    queryFn: diffFetch(''),
  })
  // The headline first: the same pair's totals without the row walk land in
  // a second or two, so the +X / Δobjects line shows while the rows align.
  const diffSumQ = useQuery<DiffData, Error>({
    queryKey: ['diff', diffPrev, asof, graftPath, canW, 'summary'],
    enabled: !!asof && !!diffPrev,
    staleTime: Infinity,
    retry: false,
    queryFn: diffFetch('&summary=1'),
  })
  const diff: DiffData | null = diffQ.data ?? null
  const diffErr = diffQ.error
  // The shown diff is the previous pair's (placeholder) or the pair is still
  // aligning: its numbers describe another pair, so the subtitle says
  // "aligning" instead, and the drawn treemap dims under a marker.
  const diffStale = diffQ.isPlaceholderData || (!diff && diffQ.isFetching)
  // What the subtitle's numbers describe: the full diff once it's this pair's,
  // else the summary (its own query — current for this key or absent).
  const diffHead: DiffData | null = diff && !diffStale ? diff : diffSumQ.data ?? null
  const [introOpen, onIntroToggle] = useFold('gcs-usage:fold2:intro')
  // Section `#hash` both ways (deep link in, scroll-spy out). Re-armed as the
  // map, meta and scans land (sections mount off different queries). The
  // sections park under the sticky pagebar (`scroll-margin-top`, app.scss).
  useHashSpy({
    ids: SECTION_IDS, hash, deps: [shownTree, meta, scans], legacy: LEGACY_ANCHORS,
    offset: () => { const el = document.getElementById('tree-map'); return el ? parseFloat(getComputedStyle(el).scrollMarginTop) || 0 : 0 },
  })
  // Map color-by (`?c=`): absent = the default (`user`); one-letter tokens.
  const [modeP, setModeP] = useUrlState('c', {
    encode: (v: string | undefined) => (v === 'user' || v === undefined ? undefined : modeToken(v as ColorMode)),
    decode: (e: string | undefined) => (e === undefined ? 'user' : MODE_TOKENS[e] ?? 'user'),
  })
  // The age chart's own color axis (`?ac=`); absent = follow the map (so a
  // pick equal to the map's effective mode is cleared, not serialized).
  const [ageModeP, setAgeModeP] = useUrlState('ac', {
    encode: (v: string | undefined) => (v === undefined ? undefined : modeToken(v as ColorMode)),
    decode: (e: string | undefined) => (e === undefined ? undefined : MODE_TOKENS[e]),
  })
  const [hlUser, setHlUser] = useUrlState('u', stringParam())
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const [theme, cycleTheme] = useTheme()
  const ident = useIdentity()
  const { units, suffixB, fmtBytes, toggleUnits, toggleSuffixB } = useUnits()
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : 'user'
  const setMode = (m: ColorMode) => setModeP(m)
  const hasAttr = (meta?.users?.length ?? 0) > 0
  const effMode: ColorMode = hasAttr ? mode : 'tree'
  // Only axes this scan can color by are offered — `user` needs attribution
  // (CoreWeave has none today), so it's absent rather than a dead button.
  const ageModes = AGE_MODES.filter(m => hasAttr || m !== 'user')
  const ageMode: ColorMode = (() => {
    const want = ageModeP && (AGE_MODES as string[]).includes(ageModeP) ? (ageModeP as ColorMode) : effMode
    return ageModes.includes(want) ? want : 'tree'
  })()
  const hl: Highlight | null = hlUser ? { user: hlUser } : null

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])

  // The drilled node, resolved against the shown tree each render (a vanished
  // path truncates to its deepest surviving ancestor). The children table
  // below the map mirrors this node, and a table row drills the map by
  // pushing onto it.
  const mapPath = useMemo((): TreeNode[] | undefined => {
    if (!shownTree) return undefined
    const path = [shownTree]
    let cur: TreeNode = shownTree
    for (const s of (drillPath ?? '').split('/').filter(Boolean)) {
      const next = cur.c?.find(c => c.n === s)
      if (!next) break
      path.push(next)
      cur = next
    }
    // A store with one bucket (CoreWeave today) opens inside it — the bucket
    // level is a single full-width box otherwise.
    if (path.length === 1 && shownTree.c?.length === 1) return [shownTree, shownTree.c[0]]
    return path
  }, [shownTree, drillPath])
  const onMapPath = (p: TreeNode[]) => setDrillPath(p.slice(1).map(n => n.n).join('/') || undefined)
  // The deepest drilled node's own subtree query — its threshold tells a
  // childless dir apart from one whose children are still loading.
  const leafQ = subtreeQs[subtreeQs.length - 1]
  // Table row → drill the map to that prefix and scroll it into view.
  const openPath = (segs: string[]) => {
    setDrillPath(segs.join('/') || undefined)
    document.querySelector('.dt-treemap')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const pickUser = (u: string) => {
    setHlUser(u)
    if (mode !== 'user') setMode('user')
  }
  const clearHl = () => {
    setHlUser(undefined)
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
      label: 'Clear user highlight',
      group: 'Highlight',
      defaultBindings: ['x'],
      handler: clearHl,
    },
    'units:toggle': {
      label: `Byte units: ${units === 'si' ? 'SI (TB) → IEC (TiB)' : 'IEC (TiB) → SI (TB)'}`,
      group: 'View',
      defaultBindings: ['i'],
      handler: toggleUnits,
    },
    'units:suffix': {
      label: `Unit suffix: ${suffixB ? 'TiB → Ti' : 'Ti → TiB'}`,
      group: 'View',
      defaultBindings: ['b'],
      handler: toggleSuffixB,
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
    ...Object.fromEntries(
      (meta?.users ?? []).map(u => [
        `user:${u.u}`,
        {
          label: `${shortName(u.u)} · ${fmtBytes(u.b)}`,
          group: 'Users',
          handler: () => pickUser(u.u),
        },
      ]),
    ),
    ...Object.fromEntries(
      scans.map(s => [
        `scan:${s}`,
        {
          label: `Scan ${s}`,
          group: 'Scans',
          handler: () => setDP(s),
        },
      ]),
    ),
  })

  const dateRange = useMemo((): DateRange | null => {
    if (!shownTree) return null
    let min = Infinity
    let max = -Infinity
    const walk = (n: TreeNode) => {
      if (n.d != null && !n.c) {
        if (n.d < min) min = n.d
        if (n.d > max) max = n.d
      }
      n.c?.forEach(walk)
    }
    walk(shownTree)
    return min < max ? { min, max } : null
  }, [shownTree])

  const catOrder = useMemo(() => {
    if (!shownTree) return []
    const catBytes = new Map<string, number>()
    for (const bucket of shownTree.c ?? [])
      for (const d of bucket.c ?? []) {
        const k = d.n.startsWith('(') ? '(other)' : d.n
        catBytes.set(k, (catBytes.get(k) ?? 0) + d.b)
      }
    return [...catBytes.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k).filter(k => k !== '(other)')
  }, [shownTree])

  // Storage-class $ estimates are GCS list prices; a store that publishes no
  // class breakdown (CoreWeave) has nothing to price — hide every $ surface.
  const hasPrices = !!meta && Object.keys(meta.class_bytes).length > 0
  const estCost = useMemo(() => {
    if (!meta || !hasPrices) return null
    const gib = (b: number) => b / 1024 ** 3
    const list = Object.entries(meta.class_bytes).reduce(
      (s, [c, b]) => s + gib(b) * (CLASS_PRICE_US[c] ?? 0.02),
      0,
    )
    return { list }
  }, [meta, hasPrices])

  const pricing = useMemo((): Pricing | null => {
    if (!meta || !hasPrices) return null
    const rates = (m?: Record<string, Record<string, number>>) =>
      m && Object.fromEntries(Object.entries(m).map(([k, cb]) => [k, ratePerByte(cb)]))
    return {
      blended: ratePerByte(meta.class_bytes),
      userRates: rates(meta.user_class_bytes),
      userMix: meta.user_class_bytes,
    }
  }, [meta, hasPrices])

  return (
    <main>
      <header>
        <div className="hrow">
          <h1>{store.title}</h1>
          {/* Nav + identity flush right as one designed cluster (no SiteNav
              here — one page). Page-scoped controls (scan/units/color-by) live
              in the sticky `.pagebar` below, not the h1 line. */}
          <span className="nav-links">
            <Link className="nav-files" to="/files">Scans</Link>
            <Link className="nav-sweep" to="/sweep">Sweep</Link>
          </span>
          {ident && (
            <div className="whoami">
              <UserChip who={ident.email} size={22} extra={<div className="uc-session"><div>signed in as <code>{ident.email}</code></div></div>} />
              <a className="logout" href="/cdn-cgi/access/logout">log out</a>
            </div>
          )}
        </div>
        {meta && (
          <p className="sub">
            <Tooltip content={`${units === 'si' ? 'SI' : 'IEC'} units${suffixB ? '' : ', bare suffix'} — click to toggle (i / b)`}><b className="dotted" style={{ cursor: 'pointer' }} onClick={toggleUnits}>{fmtBytes(meta.total_bytes)}</b></Tooltip> · <b>{fmtN(meta.total_objects)}</b> objects
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
      </header>

      <details className="prose fold" open={introOpen} onToggle={onIntroToggle}>
        <summary>
          <MdInfoOutline className="fold-icon" aria-hidden />
          <span><b>About</b> — the data &amp; color modes</span>
        </summary>
        <p>
          Storage in CoreWeave AI Object Storage (<code>s3://marin-us-east-02a</code>), from a per-object
          listing every 12 h. The treemap drills into prefixes; “color by” recolors both plots by top-level
          tree or age (older→newer). Objects under <code>tmp/ttl=&lt;N&gt;d/</code> expire by the bucket’s
          lifecycle rules; everything else is deleted only through a reviewed plan on the Sweep page
          (keep/sweep marks → plan → dry run → real run).
        </p>
      </details>

      {/* Page-scoped controls, sticky under the top edge as you scroll the
          long page: which scan, byte units, and (when the scan has
          attribution) the color-by axis. Full-bleed with a bottom border so it
          reads as a stuck bar. */}
      {meta && (
        <div className="pagebar">
          <span className="pb-grp">
            <span className="lbl">scan</span>
            {scans.length > 1 && asof ? (
              <select className="scanpick" value={asof} onChange={e => setDP(e.target.value)} aria-label="Scan date">
                {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
              </select>
            ) : (
              <b>{meta.asof}</b>
            )}
          </span>
          <Tooltip content={<>Byte units, site-wide: click toggles TiB (binary) ↔ TB (decimal); shift-click toggles the trailing B</>}>
            <button
              className="units-btn" type="button"
              onClick={e => (e.shiftKey ? toggleSuffixB : toggleUnits)()}
            >
              {(units === 'iec' ? 'Ti' : 'T') + (suffixB ? 'B' : '')}
            </button>
          </Tooltip>
          {hasAttr && (
            <span className="pb-grp colorby" role="radiogroup" aria-label="Color plots by">
              <span className="lbl">color by</span>
              {MODES.map(m => (
                <button key={m} role="radio" aria-checked={effMode === m} className={effMode === m ? 'on' : ''} onClick={() => setMode(m)}>
                  {MODE_LABELS[m]}
                </button>
              ))}
              {hl && (
                <Tooltip content="Clear highlight (x)"><button className="hlchip" onClick={clearHl}>{hlUser} ✕</button></Tooltip>
              )}
            </span>
          )}
        </div>
      )}

      {scansQ.isError && (
        <p className="tab-note" style={{ color: 'var(--s3)' }}>
          Couldn’t load snapshot data ({(scansQ.error as { status?: number })?.status === 401 ? 'not signed in — this dashboard is access-gated' : String(scansQ.error)}).
        </p>
      )}
      {shownTree ? (
        <>
          {/* `leaf` folds the canvas away (height 0): a directory its own
              subtree fetch confirmed has nothing drawable (objects only, or
              dirs under the floor). Never before that fetch answers: a
              childless dir may be a branch whose kids fell below the parent's
              pixel budget. */}
          <div id="tree-map" className={[
            'busy-host',
            mapPath && mapPath.length > 1 && !mapPath[mapPath.length - 1].c?.length && (mapPath[mapPath.length - 1].o <= 1 || leafQ?.data) ? 'leaf' : '',
            treeLoading ? 'stale' : '',
          ].filter(Boolean).join(' ')} aria-busy={mapBusy || undefined}>
            <Treemap root={shownTree} mode={effMode} userIdx={userIdx} dateRange={dateRange} hl={hl} pricing={pricing} lens={lens}
              path={mapPath} onPathChange={onMapPath} marks={marks.idx} />
            {treeLoading ? <Busy label="loading view…" /> : mapBusy ? <Busy corner label="filling in…" /> : null}
          </div>
          {/* A drilled directory with nothing drawable under it: only objects
              (not in the index — dirs only, specs/view-serving.md §3), or
              directories under this view's floor. Say so rather than show a
              blank canvas. */}
          {mapPath && mapPath.length > 1 && !mapPath[mapPath.length - 1].c?.length && leafQ?.data && (
            <p className="hint leaf-note">
              <code>{mapPath[mapPath.length - 1].n}</code> holds {fmtN(mapPath[mapPath.length - 1].o)} objects and no directory of{' '}
              {fmtBytes(leafQ.data.threshold ?? 0)} or more. Objects aren’t listed in the index — browse them under{' '}
              <Link to="/files">Scans</Link>, or press Backspace to go up.
            </p>
          )}
        </>
      ) : rootErr ? (
        <p className="loading">
          {rootErr.message.startsWith('409') ? 'no index for this scan — pick a newer scan'
            : rootErr.message.startsWith('413') ? 'this view is too wide for the index — drill in'
            : `view failed: ${rootErr.message}`}
        </p>
      ) : (
        // First paint only — before even the depth-1 tree has landed (later
        // loads hold the previous tree instead): reserve the map's slot at its
        // fetch aspect (w : 0.6w), so nothing below it jumps when it arrives.
        <div id="tree-map" className="tm-skel" aria-busy="true" aria-label="loading tree" />
      )}

      {/* The map's tabular twin: this node's children, sortable/paged, clicking
          a row drills the map into it. Foldable; open by default. */}
      {mapPath && (mapPath[mapPath.length - 1].c?.length ?? 0) > 0 && (
        <details className="tbl-fold" open>
          <summary>
            <b>Contents</b> of <code>{mapPath[mapPath.length - 1].n}</code>
            {' '}· {fmtN((mapPath[mapPath.length - 1].c ?? []).length)} entries
          </summary>
          <ChildrenTable node={mapPath[mapPath.length - 1]} segs={mapPath.slice(1).map(n => n.n)} onOpen={openPath} marks={marks} scan={asof ?? undefined} />
        </details>
      )}

      {/* Bytes per scan under the drilled prefix — one index row per scan via /api/series. */}
      <SizeOverTime scans={scans} prefix={graftPath} onPickDate={setDP} onBrush={brushRange} window={diffWindow} />

      {asof && diffPrev && (
        <section id="diff">
          <h2>Diff</h2>
          <p className="sub">
            {/* both endpoints are pickable; "after" IS the page's scan, so
                changing it moves the whole page (same as the header picker) */}
            <Tooltip content={<>The diff window's start — the size chart's shaded band reads from here to the scan. Drag on the size chart to set both ends.</>}>
              <select className="scanpick" value={diffPrev} aria-label="Diff from scan" onChange={e => pickBefore(e.target.value)}>
                {earlier.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
              </select>
            </Tooltip>
            {' '}→{' '}
            <select className="scanpick" value={asof} aria-label="Diff to scan (moves the page)" onChange={e => setDP(e.target.value)}>
              {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
            </select>
            {spanPicks.length > 0 && (
              <span className="gran spans" role="radiogroup" aria-label="Diff span (back from the after scan)">
                {spanPicks.map(({ label, ms, scan }) => (
                  <Tooltip key={label} content={`${fmtScan(scan)} → ${fmtScan(asof)}`}>
                    <button role="radio" aria-checked={diffPrev === scan} className={diffPrev === scan ? 'on' : ''}
                      onClick={() => setSpan(scan === prevScan ? undefined : ms)}>
                      {label}
                    </button>
                  </Tooltip>
                ))}
              </span>
            )}
            {diffHead ? (
              <>
                {' '}· <b className={diffHead.total_b >= diffHead.total_a ? 'grew' : 'shrank'}>
                  {(diffHead.total_b >= diffHead.total_a ? '+' : '−') + fmtBytes(Math.abs(diffHead.total_b - diffHead.total_a))}
                </b>
                {' '}· Δobjects {(diffHead.objects_b - diffHead.objects_a).toLocaleString('en-US')}
                {diffStale && <span className="loading"> · aligning the rows…</span>}
                {' '}· <Tooltip content={<>
                  <code>{graftPath || 'whole bucket'}</code> at each scan — the same drill as the map above.
                  Both scans are read at one byte floor ({fmtBytes(diffHead.threshold ?? 0)}): a directory is named on both sides or folded into
                  “(other)” on both, and one that crossed the floor is read exactly from the other scan — so every named cell’s Δ is real.
                  {diffHead.lookups_capped && <> Some small one-sided names went unread (lookup budget); they may sit in “(other)”.</>}
                </>}>
                  <span className="dotted">≈ {graftPath || 'whole bucket'}</span>
                </Tooltip>
                {diffHead.truncated && (
                  <>
                    {' '}· <Tooltip content="Largest changes shown — the diff walk was budget-capped, so the smallest movements aren’t enumerated (the totals are exact).">
                      <span className="dotted">largest changes</span>
                    </Tooltip>
                  </>
                )}
              </>
            ) : diffErr && !diffStale ? (
              <span className="tab-note">
                {' '}· {diffErr.message.startsWith('404')
                  ? <><code>{graftPath || '/'}</code> is in neither scan’s index — pick other scans or drill up.</>
                  : diffErr.message.startsWith('409')
                    ? <>one of these scans has no index yet — pick newer scans.</>
                    : <>couldn’t diff {fmtScan(diffPrev)} → {fmtScan(asof)} ({diffErr.message}).</>}
                {' '}<button type="button" className="linkish" onClick={() => diffQ.refetch()}>retry</button>
              </span>
            ) : (
              <span className="loading"> · aligning {fmtScan(diffPrev)} → {fmtScan(asof)}…</span>
            )}
          </p>
          {/* The slot keeps the treemap's height through a reload: the last
              diff dims under the marker, or (first load) a skeleton stands in. */}
          {diff && diff.rows.length > 0 && (
            <div className={diffStale ? 'diff-slot busy-host stale' : 'diff-slot busy-host'} style={{ minHeight: Math.round(canW * 0.6) }}>
              <DiffTreemap data={diff} label={store.title} />
              {diffStale && <Busy label={`aligning ${fmtScan(diffPrev)} → ${fmtScan(asof)}…`} />}
            </div>
          )}
          {diff && diff.rows.length === 0 && !diffStale && <p className="hint">No changes under this path between the two scans.</p>}
          {!diff && diffStale && <Skeleton height={Math.round(canW * 0.6)} className="diff-tm" label={`aligning ${fmtScan(diffPrev)} → ${fmtScan(asof)}…`} />}
          {diff && diff.rows.length > 0 && !diffStale && (
            <details className="tbl-fold" open>
              <summary><b>Changes</b> — the diff’s largest movements, row by row</summary>
              <DiffTable data={diff} />
            </details>
          )}
        </section>
      )}

      <section id="mtime">
        <h2>Bytes by creation date</h2>
        <p className="sub">
          When today’s objects were written — created-time strata. The chart’s color axis is its own
          (right); it follows the map’s until you pick one.
        </p>
        {age.length > 0 && (
          <AgeChart rows={age} catOrder={catOrder} mode={ageMode} onMode={m => setAgeModeP(m === effMode ? undefined : m)} modes={ageModes} userIdx={userIdx} />
        )}
      </section>

      {rules && shownTree && meta?.users && (
        <AttributionRules rules={rules} tree={shownTree} users={meta.users} />
      )}

      {meta && hasPrices && (
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

      <SpeedDial TooltipRenderer={SpeedDialTip} actions={[
        { key: 'github', label: 'GitHub', icon: <FaGithub />, href: REPO_URL },
        { key: 'lens', label: `Class lens: ${lens ? 'on' : 'off'} (s)`, icon: <MdLayers />, onClick: () => setLens(v => !v) },
        {
          key: 'theme',
          label: `Theme: ${theme}`,
          icon: theme === 'light' ? <MdLightMode /> : theme === 'dark' ? <MdDarkMode /> : <MdBrightnessAuto />,
          onClick: cycleTheme,
        },
      ]} />
      <Omnibar placeholder="Users, color modes, scans…" maxResults={15} />
      <ShortcutsModal />
    </main>
  )
}

export default function App() {
  return <AppContent />
}
