import { FaGithub } from 'react-icons/fa'
import { MdBrightnessAuto, MdDarkMode, MdLightMode } from 'react-icons/md'
import { useLocation, useNavigate } from 'react-router-dom'
import { Omnibar, ShortcutsModal, SpeedDial, useActions, type SpeedDialAction } from 'use-kbd'
import { IDENTITIES } from './identities.gen'
import { useTheme } from './theme'
import { useUnits } from './units'

export const REPO_URL = 'https://github.com/Open-Athena/marin-gcs-usage'
const CW_URL = 'https://cw-s3.oa.dev/'

// Site-wide pages, in nav order — the omnibar's "Pages" group on every route.
const PAGES: [string, string][] = [
  ['/', 'Map (home)'],
  ['/files', 'Browse scans'],
  ['/users', 'Users — mark status by owner'],
  ['/marks', 'Recent marks'],
]

// Every distinct attribution user in the registry (aliases collapse onto `u`).
const USERS = [...new Set(Object.values(IDENTITIES).map(i => i.u))].sort()

/**
 * The keyboard/omnibar chrome every page shares: the lower-right SpeedDial
 * (GitHub · theme · shortcuts), ⌘K omnibar, `?` shortcuts modal, and the
 * actions that make sense anywhere — page links, user pages, theme, units.
 * Pages add their own actions with `useActions` (one registry: the
 * HotkeysProvider sits in Root) and can push extra SpeedDial buttons via
 * `extra`.
 */
export function SiteKbd({ extra = [], placeholder = 'Pages, users, actions…' }: {
  extra?: SpeedDialAction[]
  placeholder?: string
}) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [theme, cycleTheme] = useTheme()
  const { units, suffixB, toggleUnits, toggleSuffixB } = useUnits()
  useActions({
    ...Object.fromEntries(
      PAGES.filter(([to]) => to !== pathname).map(([to, label]) => [
        `page:${to}`,
        { label, group: 'Pages', handler: () => navigate(to) },
      ]),
    ),
    'page:cw': { label: 'CoreWeave usage ↗ (cw-s3.oa.dev)', group: 'Pages', handler: () => window.open(CW_URL, '_blank', 'noreferrer') },
    'page:github': { label: 'Source on GitHub ↗', group: 'Pages', handler: () => window.open(REPO_URL, '_blank', 'noreferrer') },
    ...Object.fromEntries(
      USERS.filter(u => pathname !== `/user/${u}`).map(u => [
        `userpage:${u}`,
        { label: `${u} — storage breakdown (/user/${u})`, group: 'User pages', handler: () => navigate(`/user/${u}`) },
      ]),
    ),
    'theme:cycle': {
      label: `Theme: ${theme} (cycle)`,
      group: 'View',
      defaultBindings: ['shift+d'],
      handler: cycleTheme,
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
  })
  return (
    <>
      <SpeedDial actions={[
        { key: 'github', label: 'GitHub', icon: <FaGithub />, href: REPO_URL },
        ...extra,
        {
          key: 'theme',
          label: `Theme: ${theme}`,
          icon: theme === 'light' ? <MdLightMode /> : theme === 'dark' ? <MdDarkMode /> : <MdBrightnessAuto />,
          onClick: cycleTheme,
        },
      ]} />
      <Omnibar placeholder={placeholder} maxResults={15} />
      <ShortcutsModal />
    </>
  )
}
