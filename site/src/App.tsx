import { useEffect, useMemo, useState } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdLayers, MdLightMode } from 'react-icons/md'
import { HotkeysProvider, Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AgeChart } from './AgeChart'
import { AttributionRules } from './AttributionRules'
import { DiffTreemap } from './DiffTreemap'
import type { DiffData } from './DiffTreemap'
import { buildUserIndex } from './colors'
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
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [age, setAge] = useState<AgeRow[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [rules, setRules] = useState<Rules | null>(null)
  // Precomputed diff vs the previous scan (job/cw-diff.py); older scans lack one.
  const [diff, setDiff] = useState<DiffData | null>(null)
  // URL token matches the visible label ("age"), not the internal key ("date")
  const [modeP, setModeP] = useUrlState('c', {
    encode: (v: string | undefined) => (v === 'user' || v === undefined ? undefined : v === 'date' ? 'age' : v),
    decode: (e: string | undefined) => (e === undefined ? 'user' : e === 'age' ? 'date' : e),
  })
  const [hlUser, setHlUser] = useUrlState('u', stringParam())
  // Selected scan in the URL as short YYMMDD (`?d=260809`), kept always present
  // so each day's Slack digest can deep-link straight to its own scan.
  const [dP, setDP] = useUrlState('d', { encode: encodeScan, decode: decodeScan })
  const [scans, setScans] = useState<string[]>([])
  const asof = useMemo(() => (dP && scans.includes(dP) ? dP : scans[0] ?? null), [dP, scans])
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const [theme, cycleTheme] = useTheme()
  const ident = useIdentity()
  const { units, suffixB, fmtBytes, toggleUnits, toggleSuffixB } = useUnits()
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : 'user'
  const setMode = (m: ColorMode) => setModeP(m)
  const hasAttr = (meta?.users?.length ?? 0) > 0
  const effMode: ColorMode = hasAttr ? mode : 'tree'
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
    void fetch('/data/scans.json').then(r => r.json()).then(setScans)
    void fetch('/data/rules.json').then(r => r.json()).then(setRules).catch(() => {})
  }, [])

  useEffect(() => {
    if (!asof) return
    setTree(null)
    void fetch(`/data/${asof}/tree.json`).then(r => r.json()).then(setTree)
    void fetch(`/data/${asof}/age.json`).then(r => r.json()).then(setAge)
    void fetch(`/data/${asof}/meta.json`).then(r => r.json()).then(setMeta)
    setDiff(null)
    void fetch(`/data/${asof}/diff.json`).then(r => (r.ok ? r.json() : null)).then(setDiff).catch(() => {})
  }, [asof])

  // Keep ?d= synced to the selected scan — always baked in (even the latest
  // day) so any view is shareable and the digest deep-links resolve.
  useEffect(() => {
    if (asof && dP !== asof) setDP(asof)
  }, [asof, dP])

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

  const estCost = useMemo(() => {
    if (!meta) return null
    const gib = (b: number) => b / 1024 ** 3
    const list = Object.entries(meta.class_bytes).reduce(
      (s, [c, b]) => s + gib(b) * (CLASS_PRICE_US[c] ?? 0.02),
      0,
    )
    return { list }
  }, [meta])

  const pricing = useMemo((): Pricing | null => {
    if (!meta) return null
    const rates = (m?: Record<string, Record<string, number>>) =>
      m && Object.fromEntries(Object.entries(m).map(([k, cb]) => [k, ratePerByte(cb)]))
    return {
      blended: ratePerByte(meta.class_bytes),
      userRates: rates(meta.user_class_bytes),
      userMix: meta.user_class_bytes,
    }
  }, [meta])

  return (
    <main>
      <header>
        <div className="hrow">
          <h1>Marin CoreWeave usage</h1>
          <Link className="nav-files" to="/files" style={{ fontSize: '0.9em' }}>Browse&nbsp;scans&nbsp;→</Link>
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

      <section className="prose">
        <p>
          Storage in CoreWeave AI Object Storage (<code>s3://marin-us-east-02a</code> and friends),
          from a scheduled per-object listing. Treemap drills into prefixes; the “color by” control recolors
          both plots — by top-level tree, age (older→newer), or owning user (hi-contrast). Ownership comes
          from the <code>marin-gcs-usage</code> attribution pipeline (W&B run/config joins, executor sidecars,
          manual curation) — hover a cell for its top users, or <kbd>⌘K</kbd> to jump to a user.
        </p>
      </section>

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
          <DiffTreemap data={diff} label="Marin CoreWeave usage" />
        </section>
      )}


      <section>
        <h2>Bytes by created month</h2>
        <p className="sub">
          When today’s objects were written (created-time strata, colored by {MODE_LABELS[effMode]}).
        </p>
        {age.length > 0 && <AgeChart rows={age} catOrder={catOrder} mode={effMode} userIdx={userIdx} />}
      </section>

      {rules && tree && meta?.users && (
        <AttributionRules rules={rules} tree={tree} users={meta.users} />
      )}

      {meta && (
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
