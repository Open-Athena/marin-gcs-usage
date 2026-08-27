export interface TreeNode {
  n: string
  b: number
  o: number
  d?: number                   // bytes-weighted mean created date, epoch days
  a?: number                   // last-read epoch day (access logs; MAX over the whole subtree)
  ro?: number                  // read requests (GET/HEAD) over the access window; SUM over the subtree
  rb?: number                  // bytes served (GET/HEAD) over the access window; SUM over the subtree
  tm?: Record<string, number>  // team -> bytes (attribution overlay)
  sh?: Record<string, number>  // team -> bytes with no per-user owner ("shared" subset of tm)
  us?: [string, number][]      // top users -> bytes
  cb?: Record<string, number>  // non-STANDARD class -> bytes ("2" NL, "3" CL, "4" AR); STANDARD = b - sum
  c?: TreeNode[]
}

/** Full class mix of a node: cb plus the implied STANDARD remainder. */
export const classMix = (n: Pick<TreeNode, 'b' | 'cb'>): Record<string, number> => {
  const cold = Object.values(n.cb ?? {}).reduce((a, b) => a + b, 0)
  return { ...(n.b > cold && { 1: n.b - cold }), ...n.cb }
}

export interface UserInfo {
  u: string  // canonical user id
  t: string  // team
  b: number  // total attributed bytes
}

export const TEAM_VARS: Record<string, string> = {
  oa: '--t-oa',
  stanford: '--t-stanford',
  communal: '--t-communal',
  unknown: '--t-unknown',
  unattributed: '--t-unattr',
}

// Groups that are entirely shared (no per-user owner) — rendered washed-out,
// like the old "core (shared)" pool, and never split into a users/shared pair.
export const SHARED_GROUPS = new Set(['communal'])

// Display labels for group keys (legend items): OA stays all-caps; the rest
// are title-cased. Raw keys stay lowercase in data/URLs/tooltips.
export const GROUP_LABELS: Record<string, string> = {
  oa: 'OA',
  stanford: 'Stanford',
  communal: 'Communal',
  unknown: 'Unknown',
  unattributed: 'Unattributed',
}
export const groupLabel = (t: string): string => GROUP_LABELS[t] ?? t

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

export type ColorMode = 'team' | 'tree' | 'date' | 'read' | 'user' | 'uteam' | 'fate'

export const MODE_LABELS: Record<ColorMode, string> = {
  team: 'group',
  tree: 'tree',
  date: 'written',
  read: 'read',
  user: 'user',
  uteam: 'user·group',
  fate: 'marks',
}

export interface Meta {
  asof: string
  generated: string
  published?: string  // object lastModified (ISO) — real publish time behind a date-only id
  total_bytes: number
  total_objects: number
  class_bytes: Record<string, number>
  users?: UserInfo[]
  team_class_bytes?: Record<string, Record<string, number>>
  user_class_bytes?: Record<string, Record<string, number>>
  /** Access-log observation window (epoch days) — bounds the read-recency lens. */
  access?: { from: number; to: number }
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
  teamMix?: Record<string, Record<string, number>>  // team -> class -> bytes (tooltip breakdowns)
  userMix?: Record<string, Record<string, number>>
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

export type Units = 'si' | 'iec'

export const fmtBytesSi = (b: number, suffixB = false): string => {
  const B = suffixB ? 'B' : ''
  if (b >= 1e12) return (b / 1e12).toFixed(b >= 1e13 ? 0 : 1) + ' T' + B
  if (b >= 1e9) return (b / 1e9).toFixed(b >= 1e10 ? 0 : 1) + ' G' + B
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' M' + B
  return Math.round(b / 1e3) + ' K' + B
}

const Ki = 1024, Mi = Ki ** 2, Gi = Ki ** 3, Ti = Ki ** 4

export const fmtBytesIec = (b: number, suffixB = false): string => {
  const B = suffixB ? 'B' : ''
  if (b >= Ti) return (b / Ti).toFixed(b >= 10 * Ti ? 0 : 1) + ' Ti' + B
  if (b >= Gi) return (b / Gi).toFixed(b >= 10 * Gi ? 0 : 1) + ' Gi' + B
  if (b >= Mi) return (b / Mi).toFixed(0) + ' Mi' + B
  return Math.round(b / Ki) + ' Ki' + B
}

export const fmtN = (n: number): string => n.toLocaleString('en-US')
