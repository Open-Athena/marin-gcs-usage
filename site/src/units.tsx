import { createContext, useContext, useMemo, useState } from 'react'
import { boolParam, useUrlState } from 'use-prms'
import { fmtBytesIec, fmtBytesSi } from './types'
import type { Units } from './types'

// Byte-unit display preference. IEC (TiB) is the default — quotas are binary
// (CW's cap is 910 TiB) — with SI (TB) reachable three ways, in precedence
// order: an explicit `?si` URL param (shareable), the persisted toggle
// (localStorage), then the default. Toggling writes both, so a copied URL
// carries SI-ness but plain links stay clean.
const KEY = 'units'
const load = (): Units | null => {
  const v = localStorage.getItem(KEY)
  return v === 'si' || v === 'iec' ? v : null
}

interface UnitsCtx {
  units: Units
  fmtBytes: (b: number) => string
  toggleUnits: () => void
}

const Ctx = createContext<UnitsCtx>({ units: 'iec', fmtBytes: fmtBytesIec, toggleUnits: () => {} })

export function UnitsProvider({ children }: { children: React.ReactNode }) {
  const [siP, setSiP] = useUrlState('si', boolParam)
  const [pref, setPref] = useState<Units | null>(load)
  const units: Units = siP ? 'si' : (pref ?? 'iec')
  const value = useMemo<UnitsCtx>(
    () => ({
      units,
      fmtBytes: units === 'iec' ? fmtBytesIec : fmtBytesSi,
      toggleUnits: () => {
        const next: Units = units === 'si' ? 'iec' : 'si'
        localStorage.setItem(KEY, next)
        setPref(next)
        setSiP(next === 'si')
      },
    }),
    [units, setSiP],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useUnits = () => useContext(Ctx)
