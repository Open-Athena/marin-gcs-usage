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
}

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
