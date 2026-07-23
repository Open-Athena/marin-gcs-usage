export interface TreeNode {
  n: string
  b: number
  o: number
  tm?: Record<string, number>  // team -> bytes (attribution overlay)
  us?: [string, number][]      // top users -> bytes
  c?: TreeNode[]
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

export interface AgeRow {
  m: string   // created month, YYYY-MM
  d1: string  // top-level dir
  t?: string  // owning team (attribution-aware webdata only)
  b: number
  o: number
}

export type ColorMode = 'team' | 'tree'

export interface Meta {
  asof: string
  generated: string
  total_bytes: number
  total_objects: number
  class_bytes: Record<string, number>
}

export const fmtBytes = (b: number): string => {
  if (b >= 1e12) return (b / 1e12).toFixed(b >= 1e13 ? 0 : 1) + ' TB'
  if (b >= 1e9) return (b / 1e9).toFixed(b >= 1e10 ? 0 : 1) + ' GB'
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB'
  return Math.round(b / 1e3) + ' KB'
}

export const fmtN = (n: number): string => n.toLocaleString('en-US')
