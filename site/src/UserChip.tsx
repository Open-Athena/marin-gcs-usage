import {
  FloatingPortal,
  autoUpdate,
  flip,
  offset,
  safePolygon,
  shift,
  useDismiss,
  useFloating,
  useFocus,
  useHover,
  useInteractions,
  useRole,
} from '@floating-ui/react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Avatar, whoToHandle } from './Avatar'
import { IDENTITIES } from './identities.gen'

// Canonical display for an actor. Marks/claims carry `who` as an email or a raw
// id; canonicalize it, then read the bundled registry (short name + GitHub
// avatar). Everything shows the *short name*, never the raw email — with a
// GitHub-style hover card (interactive: you can move into it and click the link).

export const canonId = (who: string): string => whoToHandle(who)

/** Short display name — registry `name`, else the capitalized first id segment. */
export const shortName = (who: string): string => {
  const id = canonId(who)
  const rec = IDENTITIES[id]
  if (rec) return rec.name
  if (id) return id.split('-')[0].replace(/^./, c => c.toUpperCase())
  return who
}

/** Explicit GitHub handle for the real avatar, or undefined (never guessed). */
export const ghHandle = (who: string): string | undefined => IDENTITIES[canonId(who)]?.github

export const teamOf = (who: string): string | undefined => IDENTITIES[canonId(who)]?.team

/** All known users (canonical id + short name), sorted by name — for pickers. */
export const allUsers = (): { id: string; name: string }[] =>
  Object.entries(IDENTITIES)
    .map(([id, rec]) => ({ id, name: rec.name }))
    .sort((a, b) => a.name.localeCompare(b.name))

const GROUP_LABELS: Record<string, string> = {
  oa: 'Open Athena', stanford: 'Stanford', communal: 'Communal', unknown: 'Unknown',
}

/** The GitHub-style identity card shown on hover — avatar, name, group, links. */
export function UserCard({ who, extra }: { who: string; extra?: React.ReactNode }) {
  const id = canonId(who)
  const name = shortName(who)
  const gh = ghHandle(who)
  const team = teamOf(who)
  const showsRaw = who !== name && who !== id
  return (
    <div className="user-card">
      <div className="uc-head">
        <Avatar github={gh} name={name} size={38} />
        <div className="uc-id">
          <b>{name}</b>
          {team && <span className="uc-group" data-team={team}>{GROUP_LABELS[team] ?? team}</span>}
        </div>
      </div>
      {showsRaw && <div className="uc-sub">{who}</div>}
      {gh && (
        <a className="uc-link" href={`https://github.com/${gh}`} target="_blank" rel="noreferrer">
          @{gh} on GitHub
        </a>
      )}
      {id && <Link className="uc-link" to={`/user/${id}`}>storage breakdown →</Link>}
      {extra}
    </div>
  )
}

/** Avatar + short name, with an interactive hover card. The default user display. */
export function UserChip({ who, size = 18, extra }: { who: string; size?: number; extra?: React.ReactNode }) {
  const [open, setOpen] = useState(false)
  const { refs, floatingStyles, context } = useFloating({
    open,
    onOpenChange: setOpen,
    placement: 'top-start',
    middleware: [offset(6), flip(), shift({ padding: 8 })],
    whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    // safePolygon: keep the card open while the cursor travels into it, so its
    // links stay clickable (the whole point — a mouse-following tip can't be).
    useHover(context, { delay: { open: 120 }, handleClose: safePolygon() }),
    useFocus(context),
    useDismiss(context),
    useRole(context, { role: 'label' }),
  ])
  return (
    <>
      <span className="user-chip" ref={refs.setReference} tabIndex={0} {...getReferenceProps()}>
        <Avatar github={ghHandle(who)} name={shortName(who)} size={size} />
        {shortName(who)}
      </span>
      {open && (
        <FloatingPortal>
          <div className="tooltip-content user-card-pop" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()}>
            <UserCard who={who} extra={extra} />
          </div>
        </FloatingPortal>
      )}
    </>
  )
}
