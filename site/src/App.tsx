import { useEffect, useMemo, useState } from 'react'
import type { SyntheticEvent } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdInfoOutline, MdLayers, MdLightMode } from 'react-icons/md'
import { HotkeysProvider, Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AGE_MODES, AgeChart } from './AgeChart'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap } from './DiffTreemap'
import { SizeOverTime } from './SizeOverTime'
import type { DiffData } from './DiffTreemap'
import { clientDiff } from './clientDiff'
import { buildUserIndex } from './colors'
import { fmtScan, useScan } from './scan'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
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

// Per-scan payloads are immutable once published — dedupe tree fetches so the
// Changes section's "before" side, and a revisited scan, never re-download.
const treeLoads = new Map<string, Promise<TreeNode>>()
function loadTree(d: string): Promise<TreeNode> {
  let p = treeLoads.get(d)
  if (!p) {
    p = fetch(`/data/${d}/tree.json`).then(r => {
      if (!r.ok) throw new Error(`tree ${d}: ${r.status}`)
      return r.json() as Promise<TreeNode>
    })
    p.catch(() => treeLoads.delete(d))
    treeLoads.set(d, p)
  }
  return p
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

// Changes-section span presets (days back from the "after" scan).
const SPANS: [string, number][] = [['1d', 1], ['3d', 3], ['7d', 7], ['14d', 14], ['30d', 30]]
// Scan ids are UTC instants (`YYYY-MM-DDTHHMM`) or calendar dates.
const scanTime = (d: string): number => {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?/.exec(d)
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] ?? '0'), +(m[5] ?? '0')) : NaN
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
  // Scan selection (`?d=YYMMDD`) + the polling scan list; absent `?d` is a
  // first-class "latest", so a parked tab follows new scans.
  const { asof, scans, setDP } = useScan()
  // Loaded trees by scan id: the page's own (`asof`) plus, when the Changes
  // section aligns client-side, its "before" scan.
  const [trees, setTrees] = useState<Record<string, TreeNode>>({})
  const tree = asof ? trees[asof] ?? null : null
  const [age, setAge] = useState<AgeRow[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [rules, setRules] = useState<Rules | null>(null)
  // Precomputed diff vs the previous scan (job/cw-diff.py): `undefined` while
  // the fetch is in flight, `null` once it has answered "none" (older scans).
  const [bakedDiff, setBakedDiff] = useState<DiffData | null | undefined>(undefined)
  // `?dp=` — the Changes section's "before" endpoint. Absent = the baked
  // diff.json pair (previous scan → this scan, the batch job's exact walk).
  // Any other earlier scan → client-side align of the two scans' trees
  // (clientDiff). The "after" endpoint IS the page's scan (`?d=`).
  const [dpP, setDpP] = useUrlState('dp', stringParam())
  const prevScan = asof ? scans[scans.indexOf(asof) + 1] ?? null : null
  const dpValid = dpP && asof && dpP < asof && scans.includes(dpP) ? dpP : null
  // client-align when the viewer picked a non-default "before", or when this
  // scan has no baked diff.json at all (older scans; the default pair still
  // deserves a diff) — but only once the baked fetch has actually answered.
  const diffPrev =
    dpValid && dpValid !== bakedDiff?.prev ? dpValid
    : bakedDiff === null && prevScan ? prevScan
    : null
  // Span presets for the "before" endpoint: the scan *nearest* to this far
  // before "after". Scan times drift a few minutes past exact multiples
  // (8:01a, 8:07a…), so "at least N days back" would skip half a cadence; the
  // nearest scan is what a reader means by `1d`. Presets past the history's
  // reach — nearest scan more than a quarter of the span off, or already
  // claimed by a shorter preset — are dropped rather than mislabeled.
  const diffBefore = diffPrev ?? bakedDiff?.prev ?? prevScan
  const spanPicks = useMemo(() => {
    if (!asof) return []
    const t0 = scanTime(asof)
    const earlier = scans.filter(s => s < asof)
    const picks: { label: string; scan: string }[] = []
    for (const [label, days] of SPANS) {
      const cut = t0 - days * 86400_000
      let best: string | null = null
      for (const s of earlier) if (!best || Math.abs(scanTime(s) - cut) < Math.abs(scanTime(best) - cut)) best = s
      if (!best || Math.abs(scanTime(best) - cut) > days * 21600_000) continue
      if (!picks.some(p => p.scan === best)) picks.push({ label, scan: best })
    }
    return picks
  }, [asof, scans])
  const prevTree = diffPrev ? trees[diffPrev] ?? null : null
  const diff: DiffData | null = useMemo(() => {
    if (!diffPrev) return bakedDiff ?? null
    if (!prevTree || !tree || !asof) return null
    // The baked diff.json paths are bucket-relative (`checkpoints/…`); the
    // store root wraps one bucket node, so align from there to match.
    const bucketOf = (t: TreeNode) => (t.c?.length === 1 ? t.c[0] : t)
    return clientDiff(bucketOf(prevTree), bucketOf(tree), diffPrev, asof)
  }, [diffPrev, bakedDiff, prevTree, tree, asof])
  const [introOpen, onIntroToggle] = useFold('gcs-usage:fold2:intro')
  // URL token matches the visible label ("age"), not the internal key ("date")
  const [modeP, setModeP] = useUrlState('c', {
    encode: (v: string | undefined) => (v === 'user' || v === undefined ? undefined : v === 'date' ? 'age' : v),
    decode: (e: string | undefined) => (e === undefined ? 'user' : e === 'age' ? 'date' : e),
  })
  // The age chart's own color axis (`?ac=`); absent = follow the map.
  const [ageModeP, setAgeModeP] = useUrlState('ac', {
    encode: (v: string | undefined) => (v === undefined ? undefined : v === 'date' ? 'age' : v),
    decode: (e: string | undefined) => (e === undefined ? undefined : e === 'age' ? 'date' : e),
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

  useEffect(() => {
    void fetch('/data/rules.json').then(r => r.json()).then(setRules).catch(() => {})
  }, [])

  // Any scan the view needs a tree for (the page's own + the diff's "before").
  const wantTrees = useMemo(() => [asof, diffPrev].filter((d): d is string => !!d), [asof, diffPrev])
  useEffect(() => {
    for (const d of wantTrees) {
      if (trees[d]) continue
      loadTree(d).then(t => setTrees(prev => (prev[d] ? prev : { ...prev, [d]: t }))).catch(() => {})
    }
  }, [wantTrees, trees])

  useEffect(() => {
    if (!asof) return
    void fetch(`/data/${asof}/age.json`).then(r => r.json()).then(setAge)
    void fetch(`/data/${asof}/meta.json`).then(r => r.json()).then(setMeta)
    setBakedDiff(undefined)
    void fetch(`/data/${asof}/diff.json`).then(r => (r.ok ? r.json() : null)).then(setBakedDiff).catch(() => setBakedDiff(null))
  }, [asof])


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
          <h1>Marin CoreWeave usage</h1>
          {/* Nav + units + identity flush right as one designed cluster
              (CP'd from gcs `5c50fa3` / `9fe605b`; no SiteNav here — one page). */}
          <span className="nav-links">
            <Link className="nav-files" to="/files">Scans</Link>
          </span>
          <button
            className="units-btn" type="button"
            onClick={e => (e.shiftKey ? toggleSuffixB : toggleUnits)()}
            title="Byte units, site-wide: click toggles TiB (binary) ↔ TB (decimal); shift-click toggles the trailing B"
          >
            {(units === 'iec' ? 'Ti' : 'T') + (suffixB ? 'B' : '')}
          </button>
          {ident && (
            <div className="whoami">
              <UserChip who={ident.email} size={22} extra={<div className="uc-session"><div>signed in as <code>{ident.email}</code></div></div>} />
              <a className="logout" href="/cdn-cgi/access/logout">log out</a>
            </div>
          )}
        </div>
        {meta && (
          <p className="sub">
            scan{' '}
            {scans.length > 1 && asof ? (
              <select className="scanpick" value={asof} onChange={e => setDP(e.target.value)} aria-label="Scan date">
                {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
              </select>
            ) : (
              <b>{meta.asof}</b>
            )}
            {' '}· <Tooltip content={`${units === 'si' ? 'SI' : 'IEC'} units${suffixB ? '' : ', bare suffix'} — click to toggle (i / b)`}><b className="dotted" style={{ cursor: 'pointer' }} onClick={toggleUnits}>{fmtBytes(meta.total_bytes)}</b></Tooltip> · <b>{fmtN(meta.total_objects)}</b> objects
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
          Storage in CoreWeave AI Object Storage (<code>s3://marin-us-east-02a</code> and friends),
          from a scheduled per-object listing. Treemap drills into prefixes; the “color by” control recolors
          both plots — by top-level tree, age (older→newer), or owning user (hi-contrast). Ownership comes
          from the <code>marin-gcs-usage</code> attribution pipeline (W&B run/config joins, executor sidecars,
          manual curation) — hover a cell for its top users, or <kbd>⌘K</kbd> to jump to a user.
        </p>
      </details>

      {hasAttr && (
        <div className="colorctl" role="radiogroup" aria-label="Color plots by">
          <span className="lbl">color by</span>
          {MODES.map(m => (
            <button
              key={m}
              role="radio"
              aria-checked={effMode === m}
              className={effMode === m ? 'on' : ''}
              onClick={() => setMode(m)}
            >
              {MODE_LABELS[m]}
            </button>
          ))}
          {hl && (
            <button className="hlchip" onClick={clearHl} title="Clear highlight (x)">
              {hlUser} ✕
            </button>
          )}
        </div>
      )}

      {tree ? (
        <Treemap root={tree} mode={effMode} userIdx={userIdx} dateRange={dateRange} hl={hl} pricing={pricing} lens={lens}
          initialPath={tree.c?.length === 1 ? [tree, tree.c[0]] : undefined} />
      ) : (
        <p className="loading">loading tree…</p>
      )}
      {asof && prevScan && (diff || diffPrev) && (
        <section id="changes">
          <h2>Changes</h2>
          <p className="sub">
            {/* both endpoints are pickable; "after" IS the page's scan, so
                changing it moves the whole page (same as the header picker) */}
            <select className="scanpick" value={diffBefore ?? prevScan} aria-label="Diff from scan"
              onChange={e => setDpP(e.target.value === (bakedDiff?.prev ?? prevScan) ? undefined : e.target.value)}>
              {scans.filter(s => s < asof).map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
            </select>
            {' '}→{' '}
            <select className="scanpick" value={asof} aria-label="Diff to scan (moves the page)"
              onChange={e => setDP(e.target.value)}>
              {scans.map(s => <option key={s} value={s}>{fmtScan(s)}</option>)}
            </select>
            {spanPicks.length > 0 && (
              <span className="gran spans" role="radiogroup" aria-label="Diff span (back from the after scan)">
                {spanPicks.map(({ label, scan }) => (
                  <button key={label} role="radio" aria-checked={diffBefore === scan} className={diffBefore === scan ? 'on' : ''}
                    title={`${fmtScan(scan)} → ${fmtScan(asof)}`}
                    onClick={() => setDpP(scan === (bakedDiff?.prev ?? prevScan) ? undefined : scan)}>
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
                {diffPrev ? (
                  <>
                    {' '}· <Tooltip content="Aligned client-side from the two scans’ budget trees: exact for the big prefixes, approximate below the fold (small dirs hide inside “(other)” tiles, whose combined delta is still truthful). The default previous→this pair uses the batch job’s exact walk instead.">
                      <span className="dotted">≈ client-aligned</span>
                    </Tooltip>
                  </>
                ) : diff.truncated && (
                  <>
                    {' '}· <Tooltip content="Largest changes shown — the diff walk was budget-capped, so the smallest movements aren’t enumerated (the totals are exact).">
                      <span className="dotted">largest changes</span>
                    </Tooltip>
                  </>
                )}
              </>
            ) : (
              <span className="loading"> · aligning {fmtScan(diffPrev!)} → {fmtScan(asof)}…</span>
            )}
          </p>
          {diff && diff.rows.length > 0 && <DiffTreemap data={diff} label="Marin CoreWeave usage" />}
        </section>
      )}

      <SizeOverTime scans={scans} />


      <section>
        <h2>Bytes by creation date</h2>
        <p className="sub">
          When today’s objects were written — created-time strata. The chart’s color axis is its own
          (right); it follows the map’s until you pick one.
        </p>
        {age.length > 0 && (
          <AgeChart rows={age} catOrder={catOrder} mode={ageMode} onMode={m => setAgeModeP(m)} modes={ageModes} userIdx={userIdx} />
        )}
      </section>

      {rules && tree && meta?.users && (
        <AttributionRules rules={rules} tree={tree} users={meta.users} />
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
      <Omnibar placeholder="Users, color modes, scans…" maxResults={15} />
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
