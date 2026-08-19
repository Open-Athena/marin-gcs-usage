import { createContext, useContext, useMemo, useState } from 'react'
import { fmtBytesIec, fmtBytesSi } from './types'
import type { Units } from './types'

// Persisted display preference: SI (TB, matching most cloud-console UIs) vs
// IEC (TiB, matching quotas — CW's 910 TiB cap is binary). Default SI keeps
// the pre-toggle behavior for viewers who never touch it.
const KEY = 'units'
const load = (): Units => (localStorage.getItem(KEY) === 'iec' ? 'iec' : 'si')

interface UnitsCtx {
  units: Units
  fmtBytes: (b: number) => string
  toggleUnits: () => void
}

const Ctx = createContext<UnitsCtx>({ units: 'si', fmtBytes: fmtBytesSi, toggleUnits: () => {} })

export function UnitsProvider({ children }: { children: React.ReactNode }) {
  const [units, setUnits] = useState<Units>(load)
  const value = useMemo<UnitsCtx>(
    () => ({
      units,
      fmtBytes: units === 'iec' ? fmtBytesIec : fmtBytesSi,
      toggleUnits: () =>
        setUnits(u => {
          const next = u === 'si' ? 'iec' : 'si'
          localStorage.setItem(KEY, next)
          return next
        }),
    }),
    [units],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useUnits = () => useContext(Ctx)
