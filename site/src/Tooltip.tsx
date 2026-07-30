import {
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  shift,
  useDismiss,
  useFloating,
  useFocus,
  useHover,
  useInteractions,
  useRole,
} from '@floating-ui/react'
import { useState } from 'react'
import type { Placement } from '@floating-ui/react'
import { CLASS_NAMES, CLASS_PRICE_US, fmtBytes, fmtUsd } from './types'

/** Generic hover/focus tooltip (@floating-ui/react); replaces native title=. */
export function Tooltip({ content, children, placement = 'top' }: {
  content: React.ReactNode
  children: React.ReactNode
  placement?: Placement
}) {
  const [open, setOpen] = useState(false)
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement,
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useHover(context, { move: false, delay: { open: 80 } }),
    useFocus(context),
    useDismiss(context),
    useRole(context, { role: 'tooltip' }),
  ])
  return (
    <>
      <span className="tt-ref" ref={refs.setReference} tabIndex={0} {...getReferenceProps()}>
        {children}
      </span>
      {open && (
        <FloatingPortal>
          <div className="tooltip-content" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>
            {content}
          </div>
        </FloatingPortal>
      )}
    </>
  )
}

/** Per-class bytes/rate/$ breakdown table for a class-byte mix. */
export function ClassMixTip({ mix, note }: { mix: Record<string, number>; note?: string }) {
  const rows = Object.entries(mix)
    .filter(([, b]) => b > 0)
    .sort((a, b) => b[1] - a[1])
  const total = rows.reduce((s, [c, b]) => s + (b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02), 0)
  return (
    <div className="classmix">
      <table>
        <tbody>
          {rows.map(([c, b]) => (
            <tr key={c}>
              <td>{CLASS_NAMES[c] ?? c}</td>
              <td className="num">{fmtBytes(b)}</td>
              <td className="num">${CLASS_PRICE_US[c] ?? 0.02}/GiB</td>
              <td className="num">{fmtUsd((b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02))}/mo</td>
            </tr>
          ))}
          {rows.length > 1 && (
            <tr className="tot">
              <td>total</td>
              <td className="num">{fmtBytes(rows.reduce((s, [, b]) => s + b, 0))}</td>
              <td />
              <td className="num">{fmtUsd(total)}/mo</td>
            </tr>
          )}
        </tbody>
      </table>
      {note && <div className="note">{note}</div>}
    </div>
  )
}
