import { useEffect, useMemo, useState } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdLayers, MdLightMode } from 'react-icons/md'
import { HotkeysProvider, Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AgeChart } from './AgeChart'
import { AttributionRules } from './AttributionRules'
import { buildUserIndex } from './colors'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
import type { AgeRow, ColorMode, Meta, Pricing, Rules, TreeNode } from './types'
import { CLASS_NAMES, CLASS_PRICE_US, MODE_LABELS, fmtBytes, fmtN, ratePerByte } from './types'
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
  const [tree, setTree] = useState<TreeNode | null>(null)
  const [age, setAge] = useState<AgeRow[]>([])
  const [meta, setMeta] = useState<Meta | null>(null)
  const [rules, setRules] = useState<Rules | null>(null)
  // URL token matches the visible label ("age"), not the internal key ("date")
  const [modeP, setModeP] = useUrlState('c', {
    encode: (v: string | undefined) => (v === 'team' || v === undefined ? undefined : v === 'date' ? 'age' : v),
    decode: (e: string | undefined) => (e === undefined ? 'team' : e === 'age' ? 'date' : e),
  })
  const [hlUser, setHlUser] = useUrlState('u', stringParam())
  const [hlTeam, setHlTeam] = useUrlState('t', stringParam())
  // Selected scan in the URL as short YYMMDD (`?d=260809`), kept always present
  // so each day's Slack digest can deep-link straight to its own scan.
  const [dP, setDP] = useUrlState('d', {
    encode: (v: string | undefined) => (v ? v.slice(2).replace(/-/g, '') : undefined),
    decode: (e: string | undefined) =>
      e && /^\d{6}$/.test(e) ? `20${e.slice(0, 2)}-${e.slice(2, 4)}-${e.slice(4, 6)}` : undefined,
  })
  const [scans, setScans] = useState<string[]>([])
  const asof = useMemo(() => (dP && scans.includes(dP) ? dP : scans[0] ?? null), [dP, scans])
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const [theme, cycleTheme] = useTheme()
  const ident = useIdentity()
  const mode: ColorMode = (MODES as string[]).includes(modeP ?? '') ? (modeP as ColorMode) : 'team'
  const setMode = (m: ColorMode) => setModeP(m)
  const hasAttr = !!tree?.tm
  const effMode: ColorMode = hasAttr ? mode : 'tree'
  const hl: Highlight | null = hlUser ? { user: hlUser } : hlTeam ? { team: hlTeam } : null

  const userIdx = useMemo(() => buildUserIndex(meta?.users ?? []), [meta])

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
      teamRates: rates(meta.team_class_bytes),
      userRates: rates(meta.user_class_bytes),
      teamMix: meta.team_class_bytes,
      userMix: meta.user_class_bytes,
    }
  }, [meta])

  return (
    <main>
      <header>
        <div className="hrow">
          <h1>Marin GCS usage</h1>
          <Link className="nav-files" to="/files" style={{ fontSize: '0.9em' }}>Browse&nbsp;scans&nbsp;→</Link>
          {ident && (
            <div className="whoami">
              <span className="avatar" style={{ background: `hsl(${avatarHue(ident.email)} 55% 42%)` }} title={ident.name || ident.email}>
                {(ident.name || ident.email).trim()[0].toUpperCase()}
              </span>
              <span className="email" title={ident.email}>{ident.name || ident.email}</span>
              <a className="logout" href="/cdn-cgi/access/logout">log out</a>
            </div>
          )}
        </div>
        {meta && (
          <p className="sub">
            scan{' '}
            {scans.length > 1 && asof ? (
              <select className="scanpick" value={asof} onChange={e => setDP(e.target.value)} aria-label="Scan date">
                {scans.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            ) : (
              <b>{meta.asof}</b>
            )}
            {' '}· <b>{fmtBytes(meta.total_bytes)}</b> · <b>{fmtN(meta.total_objects)}</b> objects
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
          Storage across the six <code>marin-*</code> GCS buckets, from the weekly{' '}
          <a href="https://github.com/marin-community/marin/blob/main/scripts/ops/storage/" target="_blank" rel="noreferrer">Ops&nbsp;-&nbsp;Storage&nbsp;Report</a>{' '}
          scan (per-object listing, deduped). Treemap drills into prefixes; the “color by” control recolors
          both plots — by owning group (OA / Stanford / communal), top-level tree, age (older→newer), or owning
          user (hi-contrast, or hues grouped by group). Ownership comes from the{' '}
          <code>marin-gcs-usage</code> attribution pipeline (W&B run/config joins, executor sidecars, manual
          curation) — hover a cell for its group split and top users, or <kbd>⌘K</kbd> to jump to a user/group.
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
              {hlUser ?? hlTeam} ✕
            </button>
          )}
        </div>
      )}

      {tree ? (
        <Treemap root={tree} mode={effMode} userIdx={userIdx} dateRange={dateRange} hl={hl} pricing={pricing} lens={lens} />
      ) : (
        <p className="loading">loading tree…</p>
      )}

      <section>
        <h2>Bytes by created month</h2>
        <p className="sub">
          When today’s objects were written (created-time strata, colored by {MODE_LABELS[effMode]}).
        </p>
        {age.length > 0 && <AgeChart rows={age} catOrder={catOrder} mode={effMode} userIdx={userIdx} />}
      </section>

      {rules && tree?.tm && meta?.users && (
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
