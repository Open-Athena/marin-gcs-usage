export interface TreeNode {
  n: string
  b: number
  o: number
  d?: number                   // bytes-weighted mean created date, epoch days
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
  b: number  // total attributed bytes
}

export interface AgeRow {
  d: number   // created day, epoch days (site aggregates to day/week/month)
  d1: string  // top-level dir
  u?: string  // owning user
  b: number
  o: number
}

export type Granularity = 'month' | 'week' | 'day'

export type ColorMode = 'tree' | 'date' | 'user'

export const MODE_LABELS: Record<ColorMode, string> = {
  tree: 'tree',
  date: 'age',
  user: 'user',
}

export interface Meta {
  asof: string
  generated: string
  total_bytes: number
  total_objects: number
  class_bytes: Record<string, number>
  users?: UserInfo[]
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
  userRates?: Record<string, number>    // class-aware $/byte·mo per user
  userMix?: Record<string, Record<string, number>>  // user -> class -> bytes (tooltip breakdowns)
}

export const fmtUsd = (x: number): string =>
  '$' + (x >= 100 ? Math.round(x).toLocaleString('en-US') : x.toFixed(x >= 1 ? 0 : 2))

export interface RuleUser {
  u: string
  aliases: string[]
  note?: string
}

export interface RulePrefix {
  prefix: string
  user?: string
  note?: string
}

export interface Rules {
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
