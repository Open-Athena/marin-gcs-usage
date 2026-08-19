import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { FaGithub } from 'react-icons/fa'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { MdBrightnessAuto, MdDarkMode, MdLayers, MdLightMode } from 'react-icons/md'
import { HotkeysProvider, Omnibar, ShortcutsModal, SpeedDial, useActions } from 'use-kbd'
import { stringParam, useUrlState } from 'use-prms'
import { AgeChart } from './AgeChart'
import { AttributionRules } from './AttributionRules'
import { buildUserIndex } from './colors'
import { ClassMixTip, Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import type { DateRange, Highlight } from './Treemap'
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
  // Sub-daily ids are UTC instants; display in the viewer's timezone, tagged
  // with its abbreviation so nobody mistakes it for UTC. The `?d=` token stays
  // UTC (see decodeScan) — display converts, the URL doesn't.
  const dt = new Date(Date.UTC(+y, +mo - 1, +d, +hh, +(mm ?? '0')))
  return new Intl.DateTimeFormat(undefined, {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    timeZoneName: 'short',
    ...(dt.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  }).format(dt).replace(', ', ' ')
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
  // Which object store to render comes from the path (`/` = GCS, `/cw` = the
  // CoreWeave bucket), so each store is its own shareable URL with its own
  // unfurl card rather than a query param on a single page.
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const store = storeForPath(pathname)
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
  const scanQuery = <T,>(name: string) => ({
    queryKey: [name, store.key, asof],
    queryFn: () => fetch(`${store.base}/${asof}/${name}.json`).then(r => r.json() as Promise<T>),
    enabled: !!asof,
    staleTime: Infinity,
  })
  const treeQ = useQuery(scanQuery<TreeNode>('tree'))
  const ageQ = useQuery(scanQuery<AgeRow[]>('age'))
  const metaQ = useQuery(scanQuery<Meta>('meta'))
  const tree: TreeNode | null = treeQ.data ?? null
  const age: AgeRow[] = ageQ.data ?? []
  const meta: Meta | null = metaQ.data ?? null
  const [lens, setLens] = useState(false)  // treemap storage-class lens (hatch by cold fraction)
  const { units, fmtBytes, toggleUnits } = useUnits()
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
    'units:toggle': {
      label: `Byte units: ${units === 'si' ? 'SI (TB) → IEC (TiB)' : 'IEC (TiB) → SI (TB)'}`,
      group: 'View',
      defaultBindings: ['i'],
      handler: toggleUnits,
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
          {STORES.length > 1 && (
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
            <Tooltip content={<>{units === 'si' ? 'SI units (TB) — click for IEC (TiB)' : 'IEC units (TiB) — click for SI (TB)'}</>}>
              <b className="units-toggle" onClick={toggleUnits}>{fmtBytes(meta.total_bytes)}</b>
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

      <section className="prose">
        {store.key === 'cw' ? (
          <p>
            Storage in the <code>marin-us-east-02a</code> CoreWeave S3 bucket, from a per-object
            listing of the whole bucket. Treemap drills into prefixes; cells are colored by prefix,
            splitting any prefix that owns most of the bucket one level deeper (so <code>marin/</code>’s
            children get their own hues instead of one blue mass). The age chart still groups by
            top-level prefix. There’s no ownership attribution for this store yet — the group/user
            color modes and $ estimates are GCS-only, so they’re hidden here.
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
        // Remount per store: the treemap owns drill/crumb state tied to the
        // tree it mounted with, and a switch can swap `tree` without ever
        // passing through null once both payloads are cached.
        <Treemap key={store.key} root={tree} mode={effMode} userIdx={userIdx} dateRange={dateRange} hl={hl} pricing={pricing} lens={lens} scheme={store.scheme} />
      ) : (
        <p className="loading">loading tree…</p>
      )}

      <section>
        {/* Granularity is auto-picked (and user-switchable) inside AgeChart, so
            the heading stays unit-free rather than lying about "month". */}
        <h2>Bytes by created date</h2>
        <p className="sub">
          When today’s objects were written (created-time strata, colored by {MODE_LABELS[effMode]}).
        </p>
        {age.length > 0 && <AgeChart rows={age} catOrder={catOrder} mode={effMode} userIdx={userIdx} />}
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
