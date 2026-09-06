import { useQueries, useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { MdLayers } from 'react-icons/md'
import { useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AGE_MODES, AgeChart } from './AgeChart'
import { canonId, shortName, shortUserKey } from './UserChip'
import { signInUrl, useCanMark, useIdent as useIdentity } from './auth'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap } from './DiffTreemap'
import type { DiffData } from './DiffTreemap'
import { clientDiff } from './clientDiff'
import { buildUserIndex, epochDaysToDate } from './colors'
import { ChildrenTable } from './ChildrenTable'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
import { applyFilter, applyLensScale, applyNodeFilter, collectMatches, parseQuery } from './filterTree'
import { BulkBar } from './BulkBar'
import { setCurrentScan, useMarkIndex, useMarks } from './marks'
import { FATE_AXES, applyFateFilter, claimedSlice, klcSplits, lensNodePred, unattrSlice, useMyUser, userLens } from './sweep'
import type { FateAxis } from './sweep'
import { MarkHistory } from './MarkHistory'
import { SiteNav, TOPBAR_VAR } from './SiteNav'
import type { MenuEntry } from './SiteNav'
import { DAY, encodeScan, fmtScan, nearestScan, scanTime, useScan } from './scan'
import { SizeOverTime } from './SizeOverTime'
import { STORES, storeForPath } from './stores'
import { TypedPrefixModal } from './TypedPrefix'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_NAMES, CLASS_PRICE_US, MODE_LABELS, classMix, fmtN, ratePerByte } from './types'
import { SiteKbd } from './SiteKbd'
import { useMarkTotals } from './markTotals'
import { useUnits } from './units'
// The color axes on offer.
const MODES: ColorMode[] = ['fate', 'read', 'user', 'date', 'tree']

// Diff-section span presets (days back from the "after" scan).
const SPANS: [string, number][] = [['1d', 1], ['3d', 3], ['7d', 7], ['14d', 14], ['30d', 30]]

// The mark-state axis chips (`?k=` letters), in bar order.
const FATE_CHIPS: { f: FateAxis; key: string; glyph: string; color: string; tip: string }[] = [
  { f: 'keep', key: 'k', glyph: '✓', color: 'var(--mk-keep)', tip: 'Bytes under a keep decision (keep-last-ckpt counts: it splits its subtree).' },
  { f: 'sweep', key: 's', glyph: '✕', color: 'var(--mk-del)', tip: 'Bytes marked for the sweep (keep-last-ckpt counts: it splits its subtree).' },
  { f: 'unmarked', key: 'u', glyph: '○', color: 'var(--ink-2)', tip: 'The review backlog — no keep/sweep decision on the prefix or any ancestor.' },
]

// The owner axis: `?o=` is `claimed`, `unclaimed`, `me`, or a user key
// (`?o=rw`); absent = everything. Claimed = attributed to a person; unclaimed
// = the nobody-owns-it pool. A user narrows "claimed" to that person.
type OwnerMode = 'all' | 'claimed' | 'unclaimed' | 'user'

// Which of the bar's "diff window" controls to show: the start-scan picker
// only while a section that reads the window (the Diff, the size chart with
// its shaded band) is on screen.
function useSectionsVisible(ids: string[], deps: unknown[]): boolean {
  const [vis, setVis] = useState(false)
  useEffect(() => {
    const els = ids.map(id => document.getElementById(id)).filter((e): e is HTMLElement => e != null)
    if (!els.length) { setVis(false); return }
    const seen = new Map<Element, boolean>()
    const io = new IntersectionObserver(entries => {
      for (const e of entries) seen.set(e.target, e.isIntersecting)
      setVis([...seen.values()].some(Boolean))
    })
    els.forEach(el => io.observe(el))
    return () => io.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return vis
}

/** The sticky bar's current height (px) — where anchored sections park. */
const topbarH = (): number =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue(TOPBAR_VAR)) || 48

// Home-page section anchors, top to bottom — the scroll-spy keeps `#hash`
// tracking the one in view, and deep links scroll to it. Old ids keep working.
const SECTION_IDS = ['tree-map', 'tbl', 'over-time', 'marks', 'diff', 'mtime']
const LEGACY_ANCHORS: Record<string, string> = {
  'size-over-time': 'over-time', 'mark-history': 'marks', 'created-date': 'mtime', changes: 'diff',
}
// The last hash the scroll-spy itself wrote: the deep-link effect must ignore
// it, or a router-driven location change would re-scroll to wherever the
// reader already is.
let spyHash = ''
// True while a `#hash` deep link is still scrolling into place (see the
// deep-link effect); the scroll-spy holds off until then.
let deepLinkPending = false
// Reader-initiated scrolling (not the programmatic kind) — ends a deep link's pursuit.
const USER_SCROLL_EVENTS = ['wheel', 'touchmove', 'keydown'] as const

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
  const [typedOpen, setTypedOpen] = useState(false)
  // Keep the tab title in sync with the store on client-side navigation.
  useEffect(() => {
    document.title = store.title
  }, [store])
  // URL token matches the visible label ("written"/"mark"), not the internal
  // key ("date"/"fate"); old ?c=age / ?c=fate links still decode (the retired
  // group axes decode to the default).
  // ABSENT is meaningful: it means "the lens-appropriate default" (see `mode`
  // below), so switching lenses re-defaults the coloring — but an explicit
  // pick (any `?c=`) survives every lens change.
  const modeCodec = {
    encode: (v: string | undefined) => (v === undefined ? undefined : v === 'date' ? 'written' : v === 'fate' ? 'mark' : v),
    decode: (e: string | undefined) => (e === undefined ? undefined : e === 'written' || e === 'age' ? 'date' : e === 'mark' || e === 'fate' ? 'fate' : e),
  }
  const [modeP, setModeP] = useUrlState('c', modeCodec)
  // The age chart's own color axis (`?ac=`, same tokens); absent = follow the map.
  const [ageModeP, setAgeModeP] = useUrlState('ac', modeCodec)
  // Scan selection (`?d=YYMMDD`) + the polling scan list, shared with /users
  // and /user/:id via useScan (specs/scan-param-all-pages.md). Absent `?d` is
  // a first-class "latest", so a parked tab follows new scans.
  const { asof, scans, dMatches, dP, setDP, span, setSpan, setRange, scansQ } = useScan(store)
  const rulesQ = useQuery({
    queryKey: ['rules'],
    queryFn: async () => {
      const r = await fetch('/data/rules.json')
      if (!r.ok) throw new Error(`rules: ${r.status}`)
      return r.json()
    },
    retry: false,
  })
  const rules: Rules | null = rulesQ.data ?? null
  // Ledger actions record which scan the actor was viewing.
  useEffect(() => setCurrentScan(asof ?? undefined), [asof])
  const scanQuery = <T,>(name: string) => ({
    queryKey: [name, store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
  // `?f=` (name filter), `?k=` (mark axis) and `?o=` (owner axis) read early:
  // they decide whether the full artifact tree is needed at all (see treeQ
  // below). The two axes replace the old review *lenses* (`?l=todo|user|
  // unclaimed`, `?lu=`, and the `?u=` legend pin) — one orthogonal pair
  // instead of a tab row, so "unmarked ∧ unclaimed" or "sweep ∧ one user"
  // are plain combinations. Old links normalize below.
  const [fq, setFq] = useUrlState('f', stringParam())
  const [kP, setKP] = useUrlState('k', stringParam())
  const [oP, setOP] = useUrlState('o', stringParam())
  const ident = useIdentity()
  const myUser = useMyUser(ident?.email, markMode)
  // Mark axis: `?k=` ⊆ `ksu`; absent (or every letter) = no filter.
  const fateSet = useMemo((): ReadonlySet<FateAxis> | null => {
    const on = new Set(FATE_CHIPS.filter(c => (kP ?? '').includes(c.key)).map(c => c.f))
    return on.size > 0 && on.size < FATE_AXES.length && markMode ? on : null
  }, [kP, markMode])
  const toggleFate = (f: FateAxis) => {
    const next = new Set(fateSet ?? FATE_AXES)
    if (next.has(f)) next.delete(f)
    else next.add(f)
    // Switching the last one off would show nothing: roll back to all.
    setKP(next.size === FATE_AXES.length || next.size === 0 ? undefined : FATE_CHIPS.filter(c => next.has(c.f)).map(c => c.key).join(''))
  }
  // Owner axis. `me` resolves to the signed-in user's attribution id (a
  // shared `?o=me` link shows each reader their own files); an unmapped
  // email resolves to nothing, and the axis falls back to "all" with a note.
  const ownerUser: string | null =
    !markMode || !oP || oP === 'claimed' || oP === 'unclaimed' ? null
    : oP === 'me' ? myUser
    : canonId(oP)
  const ownerMode: OwnerMode =
    !markMode || !oP ? 'all' : oP === 'claimed' ? 'claimed' : oP === 'unclaimed' ? 'unclaimed' : ownerUser ? 'user' : 'all'
  const meUnmapped = markMode && oP === 'me' && !myUser
  const setOwnerUser = (u: string | undefined) => setOP(u === undefined ? undefined : u === 'me' ? 'me' : shortUserKey(canonId(u)))
  const toggleOwner = (which: 'claimed' | 'unclaimed') => {
    // Two chips: both on = all; toggling the only-on chip off rolls back to all.
    const on = ownerMode === 'all' ? new Set(['claimed', 'unclaimed']) : ownerMode === 'unclaimed' ? new Set(['unclaimed']) : new Set(['claimed'])
    if (on.has(which)) on.delete(which)
    else on.add(which)
    setOP(on.size === 2 || on.size === 0 ? undefined : on.has('claimed') ? 'claimed' : 'unclaimed')
  }
  const viewUser = ownerUser
  // tree.json is ~29MB — the estate-wide walks (name filter's match set +
  // re-aggregation, lens scoping) still need its depth, but plain browsing
  // doesn't: the map seeds from the same pixel-budget /api/subtree that
  // serves drills. So the full tree only downloads when a filter or lens is
  // active (or for stores with no path index, where it's the only source).
  // Mark mode does NOT need it any more: the root keep/sweep/undecided rollup
  // and /users come from /api/marks/totals (ledger × floor-free index,
  // server-side); drilled rollups resolve on the loaded subtree and say ≈.
  // Server-side USER lens: My files / a pinned user render as a treemap of that
  // user's bytes, served from the by-user index variant (`/api/subtree?lens=`)
  // — no tree.json. Team lenses (communal/unclaimed) and todo/name-filter stay
  // on tree.json (a broad pool overruns the floor-free lens; see
  // specs/path-agnostic-serving.md §2.3).
  const lensUser = viewUser
  const subtreeLens = store.key === 'gcs' && lensUser && !fq ? `user:${lensUser}` : null
  // A scan whose by-user variant isn't synced (e.g. the daily ran on an image
  // predating it) makes the lens subtree 500; remember that (scan, lens) as
  // broken and fall back to tree.json + the client filter, rather than sticking
  // on "loading tree…". Set by the effect after the subtree queries below.
  const [lensBrokenKey, setLensBrokenKey] = useState<string | null>(null)
  const lensKey = subtreeLens ? `${asof}:${subtreeLens}` : null
  const activeLens = subtreeLens && lensBrokenKey !== lensKey ? subtreeLens : null
  const needFullTree = !activeLens && (store.key !== 'gcs' || fq != null || fateSet != null || ownerMode !== 'all')
  // One-time legacy-param rewrite onto the two axes, so old links (Slack
  // digests, /user pages) work and re-share in the current form:
  //   ?l=todo → ?k=u · ?l=unclaimed|communal, ?t=unattributed|communal → ?o=unclaimed
  //   ?l=user[&lu=x] (and older ?mt=mine[&mu=x]) → ?o=x|me · ?u=x (legend pin) → ?o=x
  //   any other ?t= (the retired group pin) → dropped
  useEffect(() => {
    const sp = new URLSearchParams(search)
    const legacy = ['l', 'lu', 'u', 'mt', 'mu', 't']
    const t = sp.get('t')
    if (!legacy.some(k => sp.has(k))) return
    const l = sp.get('l') ?? sp.get('mt')
    const lu = sp.get('lu') ?? sp.get('mu')
    const u = sp.get('u')
    for (const k of legacy) sp.delete(k)
    if (t === 'unattributed' || t === 'communal') sp.set('o', 'unclaimed')
    if (l === 'todo') sp.set('k', 'u')
    else if (l === 'unclaimed' || l === 'communal') sp.set('o', 'unclaimed')
    else if (l === 'user' || l === 'mine') sp.set('o', lu ? shortUserKey(canonId(lu)) : 'me')
    if (u && !sp.has('o')) sp.set('o', shortUserKey(canonId(u)))
    navigate({ pathname, search: `?${sp.toString()}`, hash }, { replace: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])
  const treeQ = useQuery({ ...scanQuery<TreeNode>('tree'), enabled: !!asof && needFullTree })
  const ageQ = useQuery(scanQuery<AgeRow[]>('age'))
  const metaQ = useQuery(scanQuery<Meta>('meta'))
  // The Diff section's "before" endpoint comes from the `?d=` span (see
  // scan.ts): absent = the previous scan; a span resolves to the scan
  // *nearest* that far before "after" — scan times drift minutes past exact
  // multiples, so "at least N days back" would skip half a cadence. The
  // "after" endpoint IS the page's scan (`?d=`). Both sides align client-side
  // from `/api/subtree` at the drilled path and take the page scope (lens,
  // pinned row, `?f=`) exactly like the map — one code path for every scope;
  // the batch job's root-only `diff.json` is no longer read here.
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
  // A brush on the size chart hands back calendar dates; each resolves to the
  // scan on that date, and the pair becomes the page's `?d=` (after + span).
  const brushRange = (from: string, to: string) => {
    const toScan = scans.find(s => s.startsWith(to))
    const fromScan = scans.find(s => s.startsWith(from))
    if (!toScan || !fromScan || toScan <= fromScan) return
    setRange(toScan, spanTo(toScan, fromScan))
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
      queryKey: ['subtree', store.key, asof, p, canW, activeLens],
      enabled: !!asof,
      staleTime: Infinity,
      // Retry (not `false`): a cold isolate can transiently 500 on a lens read;
      // returning null-without-retry left the map stuck on "loading tree…".
      // Retry transient failures, but NOT a 409 (lens variant not synced for
      // this scan) — that's deterministic; fail fast so the effect below falls
      // back to tree.json + the client filter.
      retry: (n: number, e: Error) => !e.message.startsWith('409') && n < 3,
      retryDelay: (n: number) => 400 * 2 ** n,
      queryFn: async () => {
        const r = await fetch(
          `/api/subtree?date=${asof}&path=${encodeURIComponent(p)}&w=${canW}&h=${Math.round(canW * 0.6)}${activeLens ? `&lens=${activeLens}` : ''}`,
          { credentials: 'include' },
        )
        if (!r.ok) throw new Error(`${r.status}: ${(await r.text()).slice(0, 120)}`)
        return r.json() as Promise<{ tree: TreeNode }>
      },
    })),
  })
  const rootSub = subtreeQs[0]?.data?.tree ?? null
  // Lens variant missing for this scan → the root sub errored; fall back.
  const rootSubErr = subtreeQs[0]?.isError
  useEffect(() => {
    if (lensKey && rootSubErr) setLensBrokenKey(lensKey)
  }, [lensKey, rootSubErr])
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
  // Deep-link to a section via `#hash` (e.g. `…/ego-dex#over-time`). Re-runs
  // as each data source lands (sections mount off different queries), and defers
  // to the next frame so the target exists and is laid out before we scroll.
  useEffect(() => {
    if (!hash || hash === spyHash) return
    const raw = hash.slice(1)
    const id = LEGACY_ANCHORS[raw] ?? raw
    // The treemap/table lay out async and shift the page after first paint, so a
    // single deferred scroll lands in the wrong place (or a still-empty page).
    // Re-scroll over ~2s until the anchor's position stops moving.
    // While the deep link is still trying to land (sections mount as data
    // arrives — a lens's tree.json can take seconds), the scroll-spy must not
    // rewrite the hash: at scrollY 0 it would clear `#diff` before the
    // section exists.
    // Keep nudging until the anchor sits still at the sticky bar's margin
    // (cold loads shift the page for many seconds as the map, its table and
    // the diff sides land); a reader's own scroll input ends the pursuit.
    deepLinkPending = true
    let last = NaN
    let tries = 0
    const stop = () => {
      clearInterval(iv)
      deepLinkPending = false
      for (const ev of USER_SCROLL_EVENTS) window.removeEventListener(ev, stop)
    }
    const iv = setInterval(() => {
      if (++tries > 120) { stop(); return }
      const el = document.getElementById(id)
      if (!el) return
      const top = el.getBoundingClientRect().top
      if (Math.abs(top - topbarH()) < 4 && top === last) { stop(); return } // parked
      last = top
      // Instant, not smooth: this is page-load positioning, not a navigation
      // the reader watches — and a smooth animation restarted every nudge
      // (or paused in a background tab) never gets there.
      el.scrollIntoView({ behavior: 'instant', block: 'start' })
    }, 500)
    for (const ev of USER_SCROLL_EVENTS) window.addEventListener(ev, stop, { passive: true })
    return stop
  }, [hash, tree, meta, scans])
  // Scroll-spy: keep the URL fragment tracking the section in view
  // (replaceState — no history entries, no scroll jumps), so a copied URL
  // reopens roughly where the reader was.
  useEffect(() => {
    let raf = 0
    const onScroll = () => {
      if (raf || deepLinkPending) return
      raf = requestAnimationFrame(() => {
        raf = 0
        // Reference line near the top (not ⅓ viewport): a short section
        // scrolled to the top should own the hash, not its taller successor.
        const yRef = Math.min(window.innerHeight / 3, 150)
        let cur = ''
        for (const id of SECTION_IDS) {
          const el = document.getElementById(id)
          if (el && el.getBoundingClientRect().top <= yRef) cur = `#${id}`
        }
        if (window.scrollY < 40) cur = '' // parked at the top — no anchor
        if (cur === window.location.hash) return
        spyHash = cur
        history.replaceState(history.state, '', window.location.pathname + window.location.search + cur)
      })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => { window.removeEventListener('scroll', onScroll); if (raf) cancelAnimationFrame(raf) }
  }, [])
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const { fmtBytes } = useUnits()
  // The bar shows the diff window's start only while a section that reads it
  // is on screen (re-observed as those sections mount with their data).
  const rangeVisible = useSectionsVisible(['diff', 'over-time'], [asof, scans.length, tree != null, meta != null])
  // The treemap's drill path now lives in the URL *path* (below the store's own
  // route prefix), so a drilled prefix is a real shareable URL —
  // `/marin-us-central1/ego-dex`, not `/?p=marin-us-central1/ego-dex`. View
  // options stay query params (`?c`, `?mt`, …); the section stays in the `#hash`.
  const storeBase = store.path === '/' ? '' : store.path
  const drillPath = pathname.slice(storeBase.length).replace(/^\/+/, '')
  // Exact keep/sweep/undecided for the CURRENT view — estate at the root, the
  // drilled subtree once you drill (`?path=`), so the map's rollup is exact at
  // every depth, not just the root (specs/path-agnostic-serving.md §2.3).
  const drillPfx = drillPath ? `${store.scheme}${drillPath}/` : undefined
  const totalsQ = useMarkTotals(asof, markMode ? drillPfx : undefined, markMode)
  const drillTo = (segs: string[]) =>
    navigate({ pathname: segs.length ? `${storeBase}/${segs.join('/')}` : store.path, search, hash })
  // Read-recency lens domain: the access-log observation window (meta), not
  // the tree's own min/max — "no reads" is only meaningful vs when logging began.
  const readRange = useMemo((): DateRange | null =>
    meta?.access ? { min: meta.access.from, max: meta.access.to } : null,
  [meta])
  // No explicit `?c=` → a scope-appropriate default; an explicit pick always
  // wins. During the cleanup sprint the primary axis is mark state ("marks"),
  // so the fill and the keep/sweep decorations are ONE axis. A single mark
  // state or a single owner defaults to `user` instead (fate is useless on an
  // all-undecided view; on a one-owner view the interesting axis is who else
  // is in there).
  const lensDefaultMode: ColorMode =
    fateSet?.size === 1 || ownerMode === 'user' ? 'user' : markMode ? 'fate' : 'user'
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : lensDefaultMode
  const setMode = (m: ColorMode) => setModeP(m === lensDefaultMode ? undefined : m)
  const hasAttr = !!tree?.us?.length
  const effMode: ColorMode =
    (mode === 'read' && !readRange) || (mode === 'fate' && !markMode) ? 'user' : hasAttr ? mode : 'tree'
  // The age chart's color axis: an explicit `?ac=` wins; otherwise it follows
  // the map, except marks (no per-stratum value in age.json) → written. The
  // read axis needs strata that carry `a` (scans published from 8/29 on) —
  // without them it's offered disabled and the chart falls back to written.
  const ageReadRange = age.some(r => r.a != null) ? readRange : null
  // Only axes this scan can actually color by are offered (no dead buttons):
  // `read` needs `a` strata, the user axis needs `us`.
  const ageModes = AGE_MODES.filter(m => (m !== 'read' || ageReadRange) && (hasAttr || m === 'date' || m === 'tree'))
  const ageMode: ColorMode = (() => {
    const want: ColorMode = ageModeP && (AGE_MODES as string[]).includes(ageModeP) ? (ageModeP as ColorMode) : effMode === 'fate' ? 'date' : effMode
    return ageModes.includes(want) ? want : 'date'
  })()
  // The pinned highlight the map dims to: the owner axis's user (a scoped
  // subtree still contains minority co-tenants, and user coloring should dim
  // them); the pools are sliced exactly, nothing to dim.
  const hl: Highlight | null = ownerUser ? { user: ownerUser } : null
  // Any scope narrower than "everything" — sections whose data can't follow
  // it (the age chart) hide rather than show fleet-wide numbers.
  const lensScoped = fateSet != null || ownerMode !== 'all'
  // The page scope, applied to a tree: the owner axis, then the mark axis.
  // Shared by the map and the Diff section's two sides, so every widget
  // answers the same question. (The `?f=` name filter is applied before this
  // — `shownTree` for the map, per side for the diff.) `ownerDone` = a server
  // user-lens tree already IS that user's bytes; only the mark axis applies.
  const scopeTree = (t: TreeNode, ownerDone = false): TreeNode => {
    if (!ownerDone) {
      // A user keeps maximal ≥60%-owned subtrees whole (their dirs, minority
      // co-tenants dimmed by the highlight). The pools instead *slice*: every
      // node shrinks to exactly its userless (or user-owned) share — keeping
      // whole subtrees let each one's minority ride along (~0.5 PiB of user
      // bytes leaked into "Unclaimed").
      if (ownerMode === 'user') t = applyNodeFilter(t, lensNodePred(userLens(ownerUser!)))
      else if (ownerMode === 'unclaimed') t = applyLensScale(t, unattrSlice)
      else if (ownerMode === 'claimed') t = applyLensScale(t, claimedSlice)
    }
    if (fateSet) t = applyFateFilter(t, markIdx, fateSet)
    return t
  }
  const mapTree = useMemo(() => {
    if (!shownTree) return shownTree
    return scopeTree(shownTree, !!activeLens)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownTree, activeLens, fateSet, markIdx, ownerMode, ownerUser])
  // Diff sides: the drilled subtree at each endpoint (the server user-lens
  // variant when the map uses it), name-filtered and scoped like the map.
  const diffPair = useQueries({
    queries: (diffPrev && asof ? [diffPrev, asof] : []).map(d => ({
      queryKey: ['diff-side', store.key, d, graftPath, activeLens],
      staleTime: Infinity,
      retry: 1,
      queryFn: async () => {
        const r = await fetch(
          `/api/subtree?date=${d}&path=${encodeURIComponent(graftPath)}&w=1200&h=720${activeLens ? `&lens=${activeLens}` : ''}`,
          { credentials: 'include' },
        )
        if (!r.ok) throw Object.assign(new Error(`${r.status}`), { status: r.status })
        return r.json() as Promise<{ tree: TreeNode }>
      },
    })),
  })
  const diff: DiffData | null = useMemo(() => {
    const [a, b] = [diffPair[0]?.data?.tree, diffPair[1]?.data?.tree]
    if (!a || !b || !diffPrev || !asof) return null
    const side = (t: TreeNode) => scopeTree(pred ? applyFilter(t, pred) : t, !!activeLens)
    return clientDiff(side(a), side(b), diffPrev, asof)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diffPair[0]?.data, diffPair[1]?.data, diffPrev, asof, activeLens, pred, fateSet, markIdx, ownerMode, ownerUser])
  const diffMissing = diffPair.find(q => q.isError)
  // One-line description of the page scope, for the section subtitles:
  // where, then whose, then which mark states, then which names.
  const scopeParts: string[] = [
    drillPath || 'all buckets',
    ...(ownerUser ? [`${shortName(ownerUser)}’s files`] : ownerMode !== 'all' ? [ownerMode] : []),
    ...(fateSet ? [[...fateSet].join(' / ')] : []),
    ...(fq ? [`“${fq}”`] : []),
  ]
  const scopeDesc = scopeParts.join(' · ')
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
  // A server user-lens map is already just that user's bytes — nothing to dim.
  const effHl: Highlight | null = activeLens ? null : hl

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])
  const mkUsers = useMemo(
    () => (meta?.users ?? []).map(u => u.u).sort((a, b) => shortName(a).localeCompare(shortName(b))),
    [meta],
  )

  // Legend-row pins land on the owner axis (a user, or the unclaimed pool).
  // `switchMode`: a ⌘K pick from any coloring jumps to an axis where the pick
  // is visible; a legend-row click is already on such an axis and must not
  // move it.
  const pickUser = (u: string, switchMode = true) => {
    setOwnerUser(u)
    if (switchMode && mode !== 'user') setMode('user')
  }
  const pickUnclaimed = () => setOP('unclaimed')
  const clearHl = () => setOP(undefined)

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
      label: 'Clear the owner axis (everyone)',
      group: 'Scope',
      defaultBindings: ['x'],
      handler: clearHl,
    },
    'owner:me': { label: 'Owner: my files', group: 'Scope', handler: () => setOP('me') },
    'owner:claimed': { label: 'Owner: claimed only', group: 'Scope', handler: () => setOP('claimed') },
    'owner:unclaimed': { label: 'Owner: unclaimed only', group: 'Scope', handler: () => setOP('unclaimed') },
    'marks:unmarked': { label: 'Marks: unmarked only (the to-do backlog)', group: 'Scope', handler: () => setKP('u') },
    'marks:all': { label: 'Marks: every state', group: 'Scope', handler: () => setKP(undefined) },
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
          label: `${u.u} · ${fmtBytes(u.b)}`,
          group: 'Users',
          handler: () => pickUser(u.u),
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
      userRates: rates(meta.user_class_bytes),
      userMix: meta.user_class_bytes,
    }
  }, [meta, store])

  // Catch-all route: a first path segment that isn't one of the store's
  // buckets is a typo'd URL (/sweeps), not a drillable prefix — 404 it
  // instead of silently rendering the root view at a bogus address.
  const seg0 = drillPath.split('/')[0]
  if (baseTree?.c && seg0 && !baseTree.c.some(k => k.n === seg0)) {
    return (
      <main>
        <SiteNav />
        <p className="err">
          404 — <code>/{drillPath}</code> is not a bucket or page here.{' '}
          <Link to="/">home</Link> · <Link to="/sweep">sweep console</Link> · <Link to="/users">users</Link>
        </p>
      </main>
    )
  }

  const segs = drillPath.split('/').filter(Boolean)
  const scanTip = meta && (
    <div className="scan-tip">
      {asof && !/[T ]\d{2}/.test(asof) && meta.published && (
        <div>published {new Date(meta.published).toISOString().replace('T', ' ').slice(0, 16)} UTC</div>
      )}
      <div><b>{fmtBytes(meta.total_bytes)}</b> · <b>{fmtN(meta.total_objects)}</b> objects across all buckets</div>
      {estCost && (
        <div>
          est. <b>${Math.round(estCost.list).toLocaleString()}/mo</b> at list price
          <ClassMixTip mix={meta.class_bytes} note="GCS list prices (US regions) × scanned bytes; actual spend depends on the billing account's negotiated rates/credits" />
        </div>
      )}
    </div>
  )
  const ownerSelect = (
    <select className="tb-select" value={ownerMode === 'user' ? (oP === 'me' ? 'me' : ownerUser!) : ''}
      aria-label="Owner"
      onChange={e => setOwnerUser(e.target.value || undefined)}>
      <option value="">anyone</option>
      {myUser && <option value="me">me ({shortName(myUser)})</option>}
      {mkUsers.filter(u => u !== myUser).map(u => <option key={u} value={u}>{shortName(u)}</option>)}
      {ownerUser && !mkUsers.includes(ownerUser) && ownerUser !== myUser && <option value={ownerUser}>{shortName(ownerUser)}</option>}
    </select>
  )
  const menu: MenuEntry[] = markMode ? [{ key: 'typed', label: 'Mark a typed prefix…', onClick: () => setTypedOpen(true) }] : []

  return (
    <main>
      {typedOpen && <TypedPrefixModal idx={markIdx} onClose={() => setTypedOpen(false)} />}
      {/* The page scope, all of it, in the sticky bar — the same bar at the
          top of the page and mid-scroll, so every section reads against it:
          where (drill path) · when (scan, and the diff window's start while
          a section that shows it is on screen) · color axis · mark axis ·
          owner axis · name filter. */}
      <SiteNav menu={menu}>
        <span className="tb-path" aria-label="Drilled path">
          <button type="button" className={segs.length ? '' : 'here'} onClick={() => drillTo([])} title="all buckets">{segs.length ? '/' : 'all buckets'}</button>
          {segs.map((sg, i) => (
            <span key={i}>
              {i > 0 && <span className="sep">/</span>}
              <button type="button" className={i === segs.length - 1 ? 'here' : ''} onClick={() => drillTo(segs.slice(0, i + 1))} title={segs.slice(0, i + 1).join('/')}>{sg}</button>
            </span>
          ))}
        </span>
        {asof && scans.length > 1 && (
          <span className="tb-scan">
            {rangeVisible && diffPrev && (
              <>
                <Tooltip content={<>The diff window's start — the Diff and the size chart's shaded band read from here to the scan. Drag on the size chart to set both ends.</>}>
                  <select className="tb-select" value={diffPrev} aria-label="Diff from scan" onChange={e => pickBefore(e.target.value)}>
                    {earlier.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
                  </select>
                </Tooltip>
                <span className="arrow">→</span>
              </>
            )}
            <Tooltip content={scanTip ?? 'scan'}>
              <select className="tb-select scan" value={asof} onChange={e => setDP(e.target.value)} aria-label="Scan date">
                {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
              </select>
            </Tooltip>
            {diff && diff.rows.length > 0 && (
              <Tooltip content={<>{scopeDesc}: {fmtBytes(diff.total_a)} at {fmtScan(diffPrev!)} → {fmtBytes(diff.total_b)} at {fmtScan(asof)} — jump to the Diff</>}>
                <a className={`tb-delta ${diff.total_b >= diff.total_a ? 'grew' : 'shrank'}`} href="#diff">
                  {(diff.total_b >= diff.total_a ? '+' : '−') + fmtBytes(Math.abs(diff.total_b - diff.total_a))}
                </a>
              </Tooltip>
            )}
          </span>
        )}
        {hasAttr && (
          <label className="tb-ctl">
            <span className="lbl">color</span>
            <Tooltip content={
              effMode === 'date' ? <>Object <b>creation time</b>, from the bucket listings (each cell = the byte-weighted mean of its objects). GCS objects are immutable, so created ≈ last-modified.</>
              : effMode === 'read' ? <><b>Last read</b> — the most recent GET/HEAD/LIST anywhere under each cell, from the GCS usage logs (logging began {readRange ? epochDaysToDate(readRange.min) : '—'}). Brick-red = <b>never read</b> since then: prime sweep candidates.</>
              : effMode === 'fate' ? <>Effective <b>keep / sweep / undecided</b> state of every cell (the most recent covering mark wins).</>
              : effMode === 'user' ? <>Dominant <b>owner</b> of each cell; the legend lists the top users of the current view.</>
              : <>Top-level directory each cell belongs to.</>
            }>
              <select className="tb-select" value={effMode} aria-label="Color plots by" onChange={e => setMode(e.target.value as ColorMode)}>
                {MODES
                  .filter(m => (m !== 'read' || readRange) && (m !== 'fate' || markMode))
                  .map(m => <option key={m} value={m}>{MODE_LABELS[m]}</option>)}
              </select>
            </Tooltip>
          </label>
        )}
        {markMode && (
          <span className="tb-axis" role="group" aria-label="Mark states">
            <span className="lbl">marks</span>
            {FATE_CHIPS.map(c => {
              const on = !fateSet || fateSet.has(c.f)
              return (
                <Tooltip key={c.f} content={c.tip}>
                  <button type="button" className={`kind${on ? ' on' : ''}`} aria-pressed={on}
                    style={{ '--kind': c.color } as React.CSSProperties} onClick={() => toggleFate(c.f)}>
                    <span className="glyph">{c.glyph}</span>{c.f}
                  </button>
                </Tooltip>
              )
            })}
          </span>
        )}
        {markMode && hasAttr && (
          <span className="tb-axis" role="group" aria-label="Owner">
            <span className="lbl">owner</span>
            <Tooltip content="Bytes attributed to a person (W&B runs, executor sidecars, claims, curation). Pick someone below to narrow it to them.">
              <button type="button" className={`kind${ownerMode === 'all' || ownerMode === 'claimed' || ownerMode === 'user' ? ' on' : ''}`}
                aria-pressed={ownerMode !== 'unclaimed'} style={{ '--kind': 'var(--s1)' } as React.CSSProperties}
                onClick={() => toggleOwner('claimed')}>
                claimed
              </button>
            </Tooltip>
            <Tooltip content="Bytes no person owns. Claim what's yours (table below, or a pinned cell), then decide keep/sweep.">
              <button type="button" className={`kind${ownerMode === 'all' || ownerMode === 'unclaimed' ? ' on' : ''}`}
                aria-pressed={ownerMode === 'all' || ownerMode === 'unclaimed'} style={{ '--kind': 'var(--ink-2)' } as React.CSSProperties}
                onClick={() => toggleOwner('unclaimed')}>
                unclaimed
              </button>
            </Tooltip>
            {ownerSelect}
          </span>
        )}
        {hasAttr && (
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
        )}
        {pred && fq && fMatches.length > 0 && (
          <BulkBar matches={fMatches} scheme={store.scheme} query={fq} />
        )}
      </SiteNav>

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
      {marksQ.error && <p className="tab-note err">Marks unavailable: {marksQ.error.message}</p>}
      {meUnmapped && (
        <p className="tab-note">
          Your email isn't mapped to an attribution user yet — ping Ryan (or an admin can add you at{' '}
          <code>/admin/db/user_emails</code>); pick any user from the owner menu to view their files.
        </p>
      )}
      {scansQ.isError && (
        <p className="tab-note" style={{ color: 'var(--s3)' }}>
          Couldn’t load snapshot data ({(scansQ.error as { status?: number })?.status === 401 ? 'not signed in — this dashboard is access-gated' : String(scansQ.error)}).
          {' '}<a href={signInUrl()}>Sign in</a> or reload once your session is active.
        </p>
      )}

      {mapTree ? (
        <>
          {/* Remount per store: the treemap's caches are tied to the tree it
              mounted with, and a switch can swap `tree` without ever passing
              through null once both payloads are cached. */}
          <div id="tree-map"><Treemap
            key={store.key}
            root={mapTree}
            mode={effMode}
            userIdx={userIdx}
            dateRange={dateRange}
            readRange={readRange}
            hl={effHl}
            onPickUser={u => pickUser(u, false)}
            onPickUnclaimed={pickUnclaimed}
            onClearHl={clearHl}
            pricing={pricing}
            lens={lens}
            scheme={store.scheme}
            markIdx={markMode ? markIdx : undefined}
            klcIdx={markMode ? klcIdx : undefined}
            // Exact fate totals only when they describe THIS view: a server
            // user-lens map gets that user's totals; the unscoped estate gets
            // the estate totals. Any client-side scoping (a pool, a mark
            // state, a group pin) has no server-sliced totals — pass null so
            // the ≈ client walk over the scoped tree keeps numerator and
            // denominator on the same slice (estate totals over a 269 Ti
            // scope read as "undecided 777%").
            viewFates={
              activeLens && lensUser && !fateSet ? totalsQ.data?.users?.[lensUser] ?? null
              : lensScoped ? null
              : totalsQ.data?.total ?? null
            }
            path={mapPath}
            onPathChange={onMapPath}
          /></div>
          {/* The map's own listing — this node's children, narrowed to the
              mark axis (`{unmarked}` drops already-decided prefixes). */}
          {mapPath && (
            <div id="tbl"><ChildrenTable
              node={mapPath[mapPath.length - 1]}
              segs={mapPath.slice(1).map(n => n.n)}
              scheme={store.scheme}
              markIdx={markMode ? markIdx : undefined}
              fates={markMode ? fateSet : null}
              onOpen={openPath}
            /></div>
          )}
        </>
      ) : (
        <p className="loading">loading tree…</p>
      )}

      {/* With the mark axis active the series chart flips to mark-progress
          (the ledger replayed per scan — specs/lens-aware-time-series.md);
          otherwise the owner axis picks the series: that user's (or the
          claimed / unclaimed pool's) bytes per scan, from the per-scan metas.
          The age chart still hides under any scope until /api/age lands. */}
      <SizeOverTime
        scans={scans} prefix={drillPath} base={store.base} fates={fateSet}
        user={ownerUser}
        pool={ownerMode === 'unclaimed' ? 'unclaimed' : ownerMode === 'claimed' ? 'claimed' : null}
        onPickDate={setDP}
        onBrush={brushRange}
        window={diffWindow}
      />

      {markMode && (
        <MarkHistory prefix={store.scheme + drillPath} scope={drillPath || 'all buckets'} pred={pred} filterQ={fq} window={diffWindow} />
      )}

      {asof && diffPrev && (
        <section id="diff">
          <h2>Diff</h2>
          <p className="sub">
            {/* Both endpoints are the bar's pickers (the start one appears
                while this section is on screen); the presets ride here. */}
            <b>{fmtScan(diffPrev)}</b> → <b>{fmtScan(asof)}</b>
            {spanPicks.length > 0 && (
              <span className="gran spans" role="radiogroup" aria-label="Diff span (back from the after scan)">
                {spanPicks.map(({ label, ms, scan }) => (
                  <button key={label} role="radio" aria-checked={diffPrev === scan} className={diffPrev === scan ? 'on' : ''}
                    title={`${fmtScan(scan)} → ${fmtScan(asof)}`}
                    onClick={() => setSpan(scan === prevScan ? undefined : ms)}>
                    {label}
                  </button>
                ))}
              </span>
            )}
            {diff ? (
              <>
                {' '}· <b className={diff.total_b >= diff.total_a ? 'grew' : 'shrank'}>
                  {(diff.total_b >= diff.total_a ? '+' : '−') + fmtBytes(Math.abs(diff.total_b - diff.total_a))}
                </b>
                {' '}· Δobjects {(diff.objects_b - diff.objects_a).toLocaleString('en-US')}
                {' '}· <Tooltip content={<>
                  <b>{scopeDesc}</b> at each scan — the same scope as the map above (drill, lens, pinned row, name filter), so in a lens
                  a subtree that left the slice (e.g. got claimed) shows as shrunk even if its bytes didn’t move.
                  Aligned client-side from the two scans’ budget trees: exact for the big prefixes, approximate below the fold
                  (small dirs hide inside “(other)” tiles, whose combined delta is still truthful).
                </>}>
                  <span className="dotted">≈ {scopeDesc}</span>
                </Tooltip>
                {diff.truncated && (
                  <>
                    {' '}· <Tooltip content="Largest changes shown — the diff walk was budget-capped, so the smallest movements aren’t enumerated (the totals are exact).">
                      <span className="dotted">largest changes</span>
                    </Tooltip>
                  </>
                )}
              </>
            ) : diffMissing ? (
              <span className="tab-note">
                {' '}· {(diffMissing.error as { status?: number }).status === 404
                  ? <>no path index for <code>{graftPath || '/'}</code> at {fmtScan(diffPair[0]?.isError ? diffPrev : asof)} — pick another scan or drill up.</>
                  : <>couldn’t load {fmtScan(diffPair[0]?.isError ? diffPrev : asof)} ({String(diffMissing.error)}).</>}
                {' '}<button type="button" className="linkish" onClick={() => diffPair.forEach(q => q.isError && q.refetch())}>retry</button>
              </span>
            ) : (
              <span className="loading"> · aligning {fmtScan(diffPrev)} → {fmtScan(asof)}…</span>
            )}
          </p>
          {diff && diff.rows.length > 0 && <DiffTreemap data={diff} label={scopeDesc} />}
          {diff && diff.rows.length === 0 && <p className="hint">No changes in this scope between the two scans.</p>}
        </section>
      )}

      {!lensScoped && (
      <section id="mtime">
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
          <AgeChart rows={age} catOrder={catOrder} mode={ageMode} onMode={m => setAgeModeP(m)} modes={ageModes} userIdx={userIdx} readRange={ageReadRange} />
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
      {hasAttr && tree && <AttributionRules tree={tree} />}

      <SiteKbd
        placeholder="Users, color modes, scans, pages…"
        extra={[{ key: 'lens', label: `Class lens: ${lens ? 'on' : 'off'} (s)`, icon: <MdLayers />, onClick: () => setLens(v => !v) }]}
      />
    </main>
  )
}

export default function App() {
  return <AppContent />
}
