import { createContext, useContext, useMemo, useState } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { fmtBytesIec, fmtBytesSi } from './types'
import type { Units } from './types'

// Byte-unit display preference. IEC (TiB) is the default — quotas are binary
// (CW's cap is 910 TiB) — with SI (TB) reachable three ways, in precedence
// order: an explicit `?si` URL param (shareable), the persisted toggle
// (localStorage), then the default. Toggling writes both, so a copied URL
// carries SI-ness but plain links stay clean.
//
// The trailing "B" is its own preference on the same precedence ladder
// (`?nb` param > localStorage > default-on): bare `1.2 Ti` is tighter in
// dense treemap cells, but next to object counts the B disambiguates, so
// it stays opt-out.
const KEY = 'units'
const load = (): Units | null => {
  const v = localStorage.getItem(KEY)
  return v === 'si' || v === 'iec' ? v : null
}
const BKEY = 'unitsB'
const loadB = (): boolean | null => {
  const v = localStorage.getItem(BKEY)
  return v === '1' ? true : v === '0' ? false : null
}

interface UnitsCtx {
  units: Units
  suffixB: boolean
  fmtBytes: (b: number) => string
  toggleUnits: () => void
  toggleSuffixB: () => void
}

const Ctx = createContext<UnitsCtx>({
  units: 'iec',
  suffixB: true,
  fmtBytes: fmtBytesIec,
  toggleUnits: () => {},
  toggleSuffixB: () => {},
})

export function UnitsProvider({ children }: { children: React.ReactNode }) {
  const [siP, setSiP] = useUrlState('si', boolParam)
  const [nbP, setNbP] = useUrlState('nb', boolParam)
  const [pref, setPref] = useState<Units | null>(load)
  const [bPref, setBPref] = useState<boolean | null>(loadB)
  const units: Units = siP ? 'si' : (pref ?? 'iec')
  const suffixB = nbP ? false : (bPref ?? true)
  const value = useMemo<UnitsCtx>(
    () => ({
      units,
      suffixB,
      fmtBytes: (b: number) => (units === 'iec' ? fmtBytesIec : fmtBytesSi)(b, suffixB),
      toggleUnits: () => {
        const next: Units = units === 'si' ? 'iec' : 'si'
        localStorage.setItem(KEY, next)
        setPref(next)
        setSiP(next === 'si')
      },
      toggleSuffixB: () => {
        const next = !suffixB
        localStorage.setItem(BKEY, next ? '1' : '0')
        setBPref(next)
        setNbP(!next)
      },
    }),
    [units, suffixB, setSiP, setNbP],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useUnits = () => useContext(Ctx)
