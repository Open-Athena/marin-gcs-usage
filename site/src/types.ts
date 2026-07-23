export interface TreeNode {
  n: string
  b: number
  o: number
  c?: TreeNode[]
}

export interface AgeRow {
  m: string   // created month, YYYY-MM
  d1: string  // top-level dir
  b: number
  o: number
}

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
