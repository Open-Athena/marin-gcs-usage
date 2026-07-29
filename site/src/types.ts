export interface TreeNode {
  n: string
  b: number
  o: number
  d?: number                   // bytes-weighted mean created date, epoch days
  tm?: Record<string, number>  // team -> bytes (attribution overlay)
  sh?: Record<string, number>  // team -> bytes with no per-user owner ("shared" subset of tm)
  us?: [string, number][]      // top users -> bytes
  c?: TreeNode[]
}

export interface UserInfo {
  u: string  // canonical user id
  t: string  // team
  b: number  // total attributed bytes
}

export const TEAM_VARS: Record<string, string> = {
  core: '--t-core',
  stanford: '--t-stanford',
  oa: '--t-oa',
  unknown: '--t-unknown',
  unattributed: '--t-unattr',
}

export const domTeam = (n: TreeNode): string | null => {
  if (!n.tm) return null
  let best: string | null = null
  let bb = -1
  for (const [t, b] of Object.entries(n.tm)) if (b > bb) { best = t; bb = b }
  return best
}

// Washed-out variant of a team color for its "shared" (no per-user owner) subset.
export const sharedColor = (teamVar: string): string =>
  `color-mix(in oklab, var(${teamVar}) 52%, var(--panel))`

// Dominant segment of a node when teams are split into user-attributed vs
// shared halves (team color-mode rendering).
export const domTeamSeg = (n: TreeNode): { team: string; shared: boolean } | null => {
  if (!n.tm) return null
  let best: { team: string; shared: boolean } | null = null
  let bb = -1
  for (const [t, b] of Object.entries(n.tm)) {
    const s = n.sh?.[t] ?? 0
    if (b - s > bb) { bb = b - s; best = { team: t, shared: false } }
    if (s > bb) { bb = s; best = { team: t, shared: true } }
  }
  return best
}

export interface AgeRow {
  d: number   // created day, epoch days (site aggregates to day/week/month)
  d1: string  // top-level dir
  t?: string  // owning team (attribution-aware webdata only)
  u?: string  // owning user
  b: number
  o: number
}

export type Granularity = 'month' | 'week' | 'day'

export type ColorMode = 'team' | 'tree' | 'date' | 'user' | 'uteam'

export const MODE_LABELS: Record<ColorMode, string> = {
  team: 'team',
  tree: 'tree',
  date: 'age',
  user: 'user',
  uteam: 'user·team',
}

export interface Meta {
  asof: string
  generated: string
  total_bytes: number
  total_objects: number
  class_bytes: Record<string, number>
  users?: UserInfo[]
  team_class_bytes?: Record<string, Record<string, number>>
  user_class_bytes?: Record<string, Record<string, number>>
}

// GCS list prices, $/GiB·mo, US regions, by storage_class_id
export const CLASS_PRICE_US: Record<string, number> = { 1: 0.02, 2: 0.01, 3: 0.004, 4: 0.0012 }
export const CLASS_NAMES: Record<string, string> = { 1: 'Standard', 2: 'Nearline', 3: 'Coldline', 4: 'Archive' }

// blended $/byte·mo for a storage-class byte mix
export const ratePerByte = (classBytes: Record<string, number>): number => {
  let usd = 0
  let bytes = 0
  for (const [c, b] of Object.entries(classBytes)) {
    usd += (b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02)
    bytes += b
  }
  return bytes ? usd / bytes : 0
}

export interface Pricing {
  blended: number                       // $/byte·mo across the whole fleet
  teamRates?: Record<string, number>    // class-aware $/byte·mo per team
  userRates?: Record<string, number>
}

export const fmtUsd = (x: number): string =>
  '$' + (x >= 100 ? Math.round(x).toLocaleString('en-US') : x.toFixed(x >= 1 ? 0 : 2))

export interface RuleUser {
  u: string
  team: string
  aliases: string[]
  note?: string
}

export interface RulePrefix {
  prefix: string
  team: string
  user?: string
  note?: string
}

export interface Rules {
  teams: string[]
  users: RuleUser[]
  prefix_owners: RulePrefix[]
}

export const fmtBytes = (b: number): string => {
  if (b >= 1e12) return (b / 1e12).toFixed(b >= 1e13 ? 0 : 1) + ' TB'
  if (b >= 1e9) return (b / 1e9).toFixed(b >= 1e10 ? 0 : 1) + ' GB'
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB'
  return Math.round(b / 1e3) + ' KB'
}

export const fmtN = (n: number): string => n.toLocaleString('en-US')
