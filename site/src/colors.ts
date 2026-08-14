import type { UserInfo } from './types'

// ---- date gradient (viridis-like, old → new) ----

const DATE_STOPS = ['#440154', '#3b528b', '#21918c', '#5ec962', '#fde725']

const hex2rgb = (h: string): [number, number, number] => [
  parseInt(h.slice(1, 3), 16),
  parseInt(h.slice(3, 5), 16),
  parseInt(h.slice(5, 7), 16),
]

export function dateColor(t: number): string {
  const x = Math.min(1, Math.max(0, t)) * (DATE_STOPS.length - 1)
  const i = Math.min(DATE_STOPS.length - 2, Math.floor(x))
  const f = x - i
  const a = hex2rgb(DATE_STOPS[i])
  const b = hex2rgb(DATE_STOPS[i + 1])
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * f))
  return `rgb(${c[0]},${c[1]},${c[2]})`
}

export const dateGradientCss = (): string =>
  `linear-gradient(90deg, ${DATE_STOPS.join(', ')})`

export const epochDaysToMonth = (d: number): string => {
  const dt = new Date(d * 86400_000)
  return `${dt.getUTCFullYear()}-${String(dt.getUTCMonth() + 1).padStart(2, '0')}`
}

// ---- user palettes ----

// hi-contrast categorical (20 distinct hues, mid lightness so ink is computable)
export const HI_CONTRAST = [
  '#4269d0', '#efb118', '#ff725c', '#6cc5b0', '#3ca951',
  '#ff8ab7', '#a463f2', '#97bbf5', '#9c6b4e', '#9498a0',
  '#e15759', '#76b7b2', '#59a14f', '#edc949', '#b07aa1',
  '#1f77b4', '#d67195', '#17becf', '#bcbd22', '#8c564b',
]

/** user -> overall rank by bytes (index into the categorical palette) */
export type UserIndex = Map<string, number>

export function buildUserIndex(users: UserInfo[]): UserIndex {
  return new Map(users.map((u, rank) => [u.u, rank]))
}

export function userColor(u: string | null, idx: UserIndex): string {
  const rank = u != null ? idx.get(u) : undefined
  if (rank == null) return 'var(--t-unattr)'
  return HI_CONTRAST[rank % HI_CONTRAST.length]
}

// ---- ink (label color) for a computed background ----

export function inkFor(color: string): string {
  let r = 0, g = 0, b = 0
  if (color.startsWith('#')) [r, g, b] = hex2rgb(color)
  else if (color.startsWith('rgb')) {
    const m = color.match(/(\d+),(\d+),(\d+)/)
    if (m) [r, g, b] = [+m[1], +m[2], +m[3]]
  } else if (color.startsWith('hsl')) {
    const m = color.match(/hsl\(([\d.]+) ([\d.]+)% ([\d.]+)%\)/)
    if (m) {
      const [h, s, l] = [+m[1] / 360, +m[2] / 100, +m[3] / 100]
      const q = l < 0.5 ? l * (1 + s) : l + s - l * s
      const p = 2 * l - q
      const f = (t: number) => {
        t = ((t % 1) + 1) % 1
        if (t < 1 / 6) return p + (q - p) * 6 * t
        if (t < 1 / 2) return q
        if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6
        return p
      }
      ;[r, g, b] = [f(h + 1 / 3) * 255, f(h) * 255, f(h - 1 / 3) * 255]
    }
  } else return 'var(--ink)' // css var background (theme grays): use theme ink
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return lum > 0.62 ? '#1a1a19' : '#fff'
}
